# Allen Institute Software License - This software license is the 2-clause BSD
# license plus a third clause that prohibits redistribution for commercial
# purposes without further permission.
#
# Copyright 2017. Allen Institute. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Redistributions for commercial purposes are not permitted without the
# Allen Institute's written permission.
# For purposes of this license, commercial purposes is the incorporation of the
# Allen Institute's software into anything for which you will charge fees or
# other compensation. Contact terms@alleninstitute.org for commercial licensing
# opportunities.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
import h5py
import logging
import pandas as pd
import numpy as np
import six
import allensdk.brain_observatory.roi_masks as roi
import itertools
from allensdk.brain_observatory.locally_sparse_noise import LocallySparseNoise
import allensdk.brain_observatory.stimulus_info as si
import dateutil
import re
import os
from pkg_resources import parse_version
from allensdk.brain_observatory.brain_observatory_exceptions import (MissingStimulusException,
                                                                     NoEyeTrackingException)
from allensdk.api.cache import memoize

# Deprecation rerouting:
from allensdk.deprecated import deprecated
from allensdk.brain_observatory.stimulus_info import warp_stimulus_coords as si_warp_stimulus_coords
from allensdk.brain_observatory.stimulus_info import make_display_mask as si_make_display_mask
from allensdk.brain_observatory.stimulus_info import mask_stimulus_template as si_mask_stimulus_template
from allensdk.brain_observatory.brain_observatory_exceptions import EpochSeparationException

def get_epoch_mask_list(st, threshold, max_cuts=2):
    '''Convenience function to cut a stim table into multiple epochs

    :param st: input stimtable
    :param threshold: threshold on the max duration of a subepoch
    :param max_cuts: maximum number of allowed epochs to cut into
    :return: epoch_mask_list, a list of indices that define the start and end of sub-epochs
    '''

    if threshold is None:
        raise NotImplementedError('threshold not set for this type of session')

    delta = (st.start.values[1:] - st.end.values[:-1])
    cut_inds = np.where(delta > threshold)[0] + 1

    epoch_mask_list = []

    if len(cut_inds) > max_cuts:

        # See: https://gist.github.com/nicain/bce66cd073e422f07cf337b476c63be7
        #      https://github.com/AllenInstitute/AllenSDK/issues/66
        raise EpochSeparationException('more than 2 epochs cut', delta=delta)

    for ii in range(len(cut_inds)+1):

        if ii == 0:
            first_ind = st.iloc[0].start
        else:
            first_ind = st.iloc[cut_inds[ii-1]].start

        if ii == len(cut_inds):
            last_ind_inclusive = st.iloc[-1].end
        else:
            last_ind_inclusive = st.iloc[cut_inds[ii]-1].end

        epoch_mask_list.append((first_ind,last_ind_inclusive))

    return epoch_mask_list


class BrainObservatoryNwbDataSet(object):
    PIPELINE_DATASET = 'brain_observatory_pipeline'
    SUPPORTED_PIPELINE_VERSION = "2.0"

    FILE_METADATA_MAPPING = {
        'age': 'general/subject/age',
        'sex': 'general/subject/sex',
        'imaging_depth': 'general/optophysiology/imaging_plane_1/imaging depth',
        'targeted_structure': 'general/optophysiology/imaging_plane_1/location',
        'ophys_experiment_id': 'general/session_id',
        'experiment_container_id': 'general/experiment_container_id',
        'device_string': 'general/devices/2-photon microscope',
        'excitation_lambda': 'general/optophysiology/imaging_plane_1/excitation_lambda',
        'indicator': 'general/optophysiology/imaging_plane_1/indicator',
        'fov': 'general/fov',
        'genotype': 'general/subject/genotype',
        'session_start_time': 'session_start_time',
        'session_type': 'general/session_type',
        'specimen_name': 'general/specimen_name',
        'generated_by': 'general/generated_by'
    }

    STIMULUS_TABLE_TYPES = {
        'abstract_feature_series': [si.DRIFTING_GRATINGS, si.STATIC_GRATINGS],
        'indexed_time_series': [si.NATURAL_SCENES, si.LOCALLY_SPARSE_NOISE,
                                si.LOCALLY_SPARSE_NOISE_4DEG, si.LOCALLY_SPARSE_NOISE_8DEG],
        'repeated_indexed_time_series':[si.NATURAL_MOVIE_ONE, si.NATURAL_MOVIE_TWO, si.NATURAL_MOVIE_THREE]

    }

    # this array was moved before file versioning was in place
    MOTION_CORRECTION_DATASETS = [ "MotionCorrection/2p_image_series/xy_translations",
                                   "MotionCorrection/2p_image_series/xy_translation" ]

    def __init__(self, nwb_file):

        self.nwb_file = nwb_file
        self.pipeline_version = None

        if os.path.exists(self.nwb_file):
            meta = self.get_metadata()
            if meta and 'pipeline_version' in meta:
                pipeline_version_str = meta['pipeline_version']
                self.pipeline_version = parse_version(pipeline_version_str)

                if self.pipeline_version > parse_version(self.SUPPORTED_PIPELINE_VERSION):
                    logging.warning("File %s has a pipeline version newer than the version supported by this class (%s vs %s)."
                                    " Please update your AllenSDK." % (nwb_file, pipeline_version_str, self.SUPPORTED_PIPELINE_VERSION))

        self._stimulus_search = None

    def get_stimulus_epoch_table(self):
        '''Returns a pandas dataframe that summarizes the stimulus epoch duration for each acquisition time index in
        the experiment

        Parameters
        ----------
        None

        Returns
        -------
        timestamps: 2D numpy array
            Timestamp for each fluorescence sample

        traces: 2D numpy array
            Fluorescence traces for each cell
        '''


        # These are thresholds used by get_epoch_mask_list to set a maximum limit on the delta aqusistion frames to
        #  count as different trials (rows in the stim table).  This helps account for dropped frames, so that they dont
        #  cause the cutting of an entire experiment into too many stimulus epochs.  If these thresholds are too low,
        #  the assert statment in get_epoch_mask_list will halt execution.  In that case, make a bug report!.
        threshold_dict = {si.THREE_SESSION_A:32+7,
                          si.THREE_SESSION_B:15,
                          si.THREE_SESSION_C:7,
                          si.THREE_SESSION_C2:7}

        stimulus_table_dict = {}
        for stimulus in self.list_stimuli():

            stimulus_table_dict[stimulus] = self.get_stimulus_table(stimulus)

            if stimulus == si.SPONTANEOUS_ACTIVITY:
                stimulus_table_dict[stimulus]['frame'] = 0

        interval_list = []
        interval_stimulus_dict = {}
        for stimulus in self.list_stimuli():
            stimulus_interval_list = get_epoch_mask_list(stimulus_table_dict[stimulus], threshold=threshold_dict.get(self.get_session_type(), None))
            for stimulus_interval in stimulus_interval_list:
                interval_stimulus_dict[stimulus_interval] = stimulus
            interval_list += stimulus_interval_list
        interval_list.sort(key=lambda x: x[0])

        stimulus_signature_list = ['gap']
        duration_signature_list = [int(interval_list[0][0])]
        interval_signature_list = [(0,int(interval_list[0][0]))]
        for ii, interval in enumerate(interval_list):
            stimulus_signature_list.append(interval_stimulus_dict[interval])
            duration_signature_list.append(int(interval[1] - interval[0]))
            interval_signature_list.append((int(interval[0]), int(interval[1])))

            if ii != len(interval_list)-1:
                stimulus_signature_list.append('gap')
                duration_signature_list.append((int(interval_list[ii+1][0] - interval_list[ii][1])))
                interval_signature_list.append((int(interval_list[ii][1]), int(interval_list[ii+1][0])))

        stimulus_signature_list.append('gap')
        interval_signature_list.append((int(interval_list[-1][1]), len(self.get_fluorescence_timestamps())))
        duration_signature_list.append(interval_signature_list[-1][1]-interval_signature_list[-1][0])

        interval_df = pd.DataFrame({'stimulus':stimulus_signature_list,
                                    'duration':duration_signature_list,
                                    'interval':interval_signature_list})

        # Gaps are ininformative; remove them:
        interval_df = interval_df[interval_df.stimulus != 'gap']
        interval_df['start'] = [x[0] for x in interval_df['interval'].values]
        interval_df['end'] = [x[1] for x in interval_df['interval'].values]

        interval_df.reset_index(inplace=True, drop=True)
        interval_df.drop(['interval', 'duration'], axis=1, inplace=True)
        return interval_df


    def get_fluorescence_traces(self, cell_specimen_ids=None):
        ''' Returns an array of fluorescence traces for all ROI and
        the timestamps for each datapoint

        Parameters
        ----------
        cell_specimen_ids: list or array (optional)
            List of cell IDs to return traces for. If this is None (default)
            then all are returned

        Returns
        -------
        timestamps: 2D numpy array
            Timestamp for each fluorescence sample

        traces: 2D numpy array
            Fluorescence traces for each cell
        '''
        timestamps = self.get_fluorescence_timestamps()
        with h5py.File(self.nwb_file, 'r') as f:
            ds = f['processing'][self.PIPELINE_DATASET][
                'Fluorescence']['imaging_plane_1']['data']

            if cell_specimen_ids is None:
                cell_traces = ds.value
            else:
                inds = self.get_cell_specimen_indices(cell_specimen_ids)
                cell_traces = ds[inds, :]

        return timestamps, cell_traces

    def get_fluorescence_timestamps(self):
        ''' Returns an array of timestamps in seconds for the fluorescence traces '''

        with h5py.File(self.nwb_file, 'r') as f:
            timestamps = f['processing'][self.PIPELINE_DATASET][
                'Fluorescence']['imaging_plane_1']['timestamps'].value
        return timestamps

    def get_neuropil_traces(self, cell_specimen_ids=None):
        ''' Returns an array of neuropil fluorescence traces for all ROIs
        and the timestamps for each datapoint

        Parameters
        ----------
        cell_specimen_ids: list or array (optional)
            List of cell IDs to return traces for. If this is None (default)
            then all are returned

        Returns
        -------
        timestamps: 2D numpy array
            Timestamp for each fluorescence sample

        traces: 2D numpy array
            Neuropil fluorescence traces for each cell
        '''

        timestamps = self.get_fluorescence_timestamps()

        with h5py.File(self.nwb_file, 'r') as f:
            if self.pipeline_version >= parse_version("2.0"):
                ds = f['processing'][self.PIPELINE_DATASET][
                    'Fluorescence']['imaging_plane_1_neuropil_response']['data']
            else:
                ds = f['processing'][self.PIPELINE_DATASET][
                    'Fluorescence']['imaging_plane_1']['neuropil_traces']

            if cell_specimen_ids is None:
                np_traces = ds.value
            else:
                inds = self.get_cell_specimen_indices(cell_specimen_ids)
                np_traces = ds[inds, :]

        return timestamps, np_traces


    def get_neuropil_r(self, cell_specimen_ids=None):
        ''' Returns a scalar value of r for neuropil correction of flourescence traces

        Parameters
        ----------
        cell_specimen_ids: list or array (optional)
            List of cell IDs to return traces for. If this is None (default)
            then results for all are returned

        Returns
        -------
        r: 1D numpy array, len(r)=len(cell_specimen_ids)
            Scalar for neuropil subtraction for each cell
        '''

        with h5py.File(self.nwb_file, 'r') as f:
            if self.pipeline_version >= parse_version("2.0"):
                r_ds = f['processing'][self.PIPELINE_DATASET][
                    'Fluorescence']['imaging_plane_1_neuropil_response']['r']
            else:
                r_ds = f['processing'][self.PIPELINE_DATASET][
                    'Fluorescence']['imaging_plane_1']['r']

            if cell_specimen_ids is None:
                r = r_ds.value
            else:
                inds = self.get_cell_specimen_indices(cell_specimen_ids)
                r = r_ds[inds]

        return r

    def get_demixed_traces(self, cell_specimen_ids=None):
        ''' Returns an array of demixed fluorescence traces for all ROIs
        and the timestamps for each datapoint

        Parameters
        ----------
        cell_specimen_ids: list or array (optional)
            List of cell IDs to return traces for. If this is None (default)
            then all are returned

        Returns
        -------
        timestamps: 2D numpy array
            Timestamp for each fluorescence sample

        traces: 2D numpy array
            Demixed fluorescence traces for each cell
        '''

        timestamps = self.get_fluorescence_timestamps()

        with h5py.File(self.nwb_file, 'r') as f:
            ds = f['processing'][self.PIPELINE_DATASET][
                'Fluorescence']['imaging_plane_1_demixed_signal']['data']
            if cell_specimen_ids is None:
                traces = ds.value
            else:
                inds = self.get_cell_specimen_indices(cell_specimen_ids)
                traces = ds[inds, :]

        return timestamps, traces

    def get_corrected_fluorescence_traces(self, cell_specimen_ids=None):
        ''' Returns an array of demixed and neuropil-corrected fluorescence traces
        for all ROIs and the timestamps for each datapoint

        Parameters
        ----------
        cell_specimen_ids: list or array (optional)
            List of cell IDs to return traces for. If this is None (default)
            then all are returned

        Returns
        -------
        timestamps: 2D numpy array
            Timestamp for each fluorescence sample

        traces: 2D numpy array
            Corrected fluorescence traces for each cell
        '''

        # starting in version 2.0, neuropil correction follows trace demixing
        if self.pipeline_version >= parse_version("2.0"):
            timestamps, cell_traces = self.get_demixed_traces(cell_specimen_ids)
        else:
            timestamps, cell_traces = self.get_fluorescence_traces(cell_specimen_ids)

        r = self.get_neuropil_r(cell_specimen_ids)

        _, neuropil_traces = self.get_neuropil_traces(cell_specimen_ids)

        fc = cell_traces - neuropil_traces * r[:, np.newaxis]

        return timestamps, fc

    def get_cell_specimen_indices(self, cell_specimen_ids=None):
        ''' Given a list of cell specimen ids, return their index based on their order in this file.

        Parameters
        ----------
        cell_specimen_ids: list of cell specimen ids

        '''

        all_cell_specimen_ids = list(self.get_cell_specimen_ids())

        try:
            inds = [list(all_cell_specimen_ids).index(i)
                    for i in cell_specimen_ids]
        except ValueError as e:
            raise ValueError("Cell specimen not found (%s)" % str(e))

        return inds

    def get_dff_traces(self, cell_specimen_ids=None):
        ''' Returns an array of dF/F traces for all ROIs and
        the timestamps for each datapoint

        Parameters
        ----------
        cell_specimen_ids: list or array (optional)
            List of cell IDs to return data for. If this is None (default)
            then all are returned

        Returns
        -------
        timestamps: 2D numpy array
            Timestamp for each fluorescence sample

        dF/F: 2D numpy array
            dF/F values for each cell
        '''
        with h5py.File(self.nwb_file, 'r') as f:
            dff_ds = f['processing'][self.PIPELINE_DATASET][
                'DfOverF']['imaging_plane_1']

            timestamps = dff_ds['timestamps'].value

            if cell_specimen_ids is None:
                cell_traces = dff_ds['data'].value
            else:
                inds = self.get_cell_specimen_indices(cell_specimen_ids)
                cell_traces = dff_ds['data'][inds, :]

        return timestamps, cell_traces

    def get_roi_ids(self):
        ''' Returns an array of IDs for all ROIs in the file

        Returns
        -------
        ROI IDs: list
        '''
        with h5py.File(self.nwb_file, 'r') as f:
            roi_id = f['processing'][self.PIPELINE_DATASET][
                'ImageSegmentation']['roi_ids'].value
        return roi_id

    def get_cell_specimen_ids(self):
        ''' Returns an array of cell IDs for all cells in the file

        Returns
        -------
        cell specimen IDs: list
        '''
        with h5py.File(self.nwb_file, 'r') as f:
            cell_id = f['processing'][self.PIPELINE_DATASET][
                'ImageSegmentation']['cell_specimen_ids'].value
        return cell_id

    def get_session_type(self):
        ''' Returns the type of experimental session, presently one of the
        following: three_session_A, three_session_B, three_session_C

        Returns
        -------
        session type: string
        '''
        with h5py.File(self.nwb_file, 'r') as f:
            session_type = f['general/session_type'].value
        return session_type.decode('utf-8')

    def get_max_projection(self):
        '''Returns the maximum projection image for the 2P movie.

        Returns
        -------
        max projection: np.ndarray
        '''

        with h5py.File(self.nwb_file, 'r') as f:
            max_projection = f['processing'][self.PIPELINE_DATASET]['ImageSegmentation'][
                'imaging_plane_1']['reference_images']['maximum_intensity_projection_image']['data'].value
        return max_projection

    def list_stimuli(self):
        ''' Return a list of the stimuli presented in the experiment.

        Returns
        -------
        stimuli: list of strings
        '''

        with h5py.File(self.nwb_file, 'r') as f:
            keys = list(f["stimulus/presentation/"].keys())
        return [ k.replace('_stimulus', '') for k in keys ]

    def get_stimulus_table(self, stimulus_name):
        ''' Return a stimulus table given a stimulus name '''
        if stimulus_name in self.STIMULUS_TABLE_TYPES['abstract_feature_series']:
            return _get_abstract_feature_series_stimulus_table(self.nwb_file, stimulus_name + "_stimulus")
        elif stimulus_name in self.STIMULUS_TABLE_TYPES['indexed_time_series']:
            return _get_indexed_time_series_stimulus_table(self.nwb_file, stimulus_name)
        elif stimulus_name in self.STIMULUS_TABLE_TYPES['repeated_indexed_time_series']:
            return _get_repeated_indexed_time_series_stimulus_table(self.nwb_file, stimulus_name)
        elif stimulus_name == 'spontaneous':
            return self.get_spontaneous_activity_stimulus_table()
        elif stimulus_name == 'master':

            epoch_table = self.get_stimulus_epoch_table()

            stimulus_table_dict = {}
            for stimulus in self.list_stimuli():
                stimulus_table_dict[stimulus] = self.get_stimulus_table(stimulus)

            table_list = []
            for stimulus in self.list_stimuli():
                curr_stimtable = stimulus_table_dict[stimulus]

                for _, row in epoch_table[epoch_table['stimulus'] == stimulus].iterrows():

                    epoch_start_ind, epoch_end_ind = row['start'], row['end']
                    curr_subtable = curr_stimtable[(epoch_start_ind <= curr_stimtable['start']) &
                                                   (curr_stimtable['end'] <= epoch_end_ind)].copy()
                    curr_subtable['stimulus'] = stimulus
                    table_list.append(curr_subtable)

            table_list = sorted(table_list, key=lambda t: t.iloc[0]['start'])

            new_table = pd.concat(table_list)
            new_table.reset_index(drop=True, inplace=True)

            return new_table



        else:
            raise IOError(
                "Could not find a stimulus table named '%s'" % stimulus_name)

    def get_spontaneous_activity_stimulus_table(self):
        ''' Return the spontaneous activity stimulus table, if it exists.

        Returns
        -------
        stimulus table: pd.DataFrame
        '''
        k = "stimulus/presentation/spontaneous_stimulus"
        with h5py.File(self.nwb_file, 'r') as f:
            events = f[k + '/data'].value
            frame_dur = f[k + '/frame_duration'].value

        start_inds = np.where(events == 1)
        stop_inds = np.where(events == -1)

        if len(start_inds) != len(stop_inds):
            raise Exception(
                "inconsistent start and time times in spontaneous activity stimulus table")

        stim_data = np.column_stack(
            [frame_dur[start_inds, 0].T, frame_dur[stop_inds, 0].T]).astype(int)

        stimulus_table = pd.DataFrame(stim_data, columns=['start', 'end'])

        return stimulus_table

    @memoize
    def get_stimulus_template(self, stimulus_name):
        ''' Return an array of the stimulus template for the specified stimulus.

        Parameters
        ----------
        stimulus_name: string
            Must be one of the strings returned by list_stimuli().

        Returns
        -------
        stimulus table: pd.DataFrame
        '''
        stim_name = stimulus_name + "_image_stack"
        with h5py.File(self.nwb_file, 'r') as f:
            image_stack = f['stimulus']['templates'][stim_name]['data'].value
        return image_stack

    def get_locally_sparse_noise_stimulus_template(self,
                                                   stimulus,
                                                   mask_off_screen=True):
        ''' Return an array of the stimulus template for the specified stimulus.

        Parameters
        ----------
        stimulus: string
           Which locally sparse noise stimulus to retrieve.  Must be one of:
               stimulus_info.LOCALLY_SPARSE_NOISE
               stimulus_info.LOCALLY_SPARSE_NOISE_4DEG
               stimulus_info.LOCALLY_SPARSE_NOISE_8DEG

        mask_off_screen: boolean
           Set off-screen regions of the stimulus to LocallySparseNoise.LSN_OFF_SCREEN.

        Returns
        -------
        tuple: (template, off-screen mask)
        '''

        if stimulus not in si.LOCALLY_SPARSE_NOISE_DIMENSIONS:
            raise KeyError("%s is not a known locally sparse noise stimulus" % stimulus)

        template = self.get_stimulus_template(stimulus)

        # build mapping from template coordinates to display coordinates
        template_shape = si.LOCALLY_SPARSE_NOISE_DIMENSIONS[stimulus]
        template_shape = [ template_shape[1], template_shape[0] ]

        template_display_shape = (1260, 720)
        display_shape = (1920, 1200)

        scale = [
            float(template_shape[0]) / float(template_display_shape[0]),
            float(template_shape[1]) / float(template_display_shape[1])
        ]
        offset = [
            -(display_shape[0] - template_display_shape[0]) * 0.5,
            -(display_shape[1] - template_display_shape[1]) * 0.5
        ]

        x, y = np.meshgrid(np.arange(display_shape[0]), np.arange(
            display_shape[1]), indexing='ij')
        template_display_coords = np.array([(x + offset[0]) * scale[0] - 0.5,
                                            (y + offset[1]) * scale[1] - 0.5],
                                           dtype=float)
        template_display_coords = np.rint(template_display_coords).astype(int)

        # build mask
        template_mask, template_frac = mask_stimulus_template(
            template_display_coords, template_shape)

        if mask_off_screen:
            template[:, ~template_mask.T] = LocallySparseNoise.LSN_OFF_SCREEN

        return template, template_mask.T

    def get_roi_mask_array(self, cell_specimen_ids=None):
        ''' Return a numpy array containing all of the ROI masks for requested cells.
        If cell_specimen_ids is omitted, return all masks.

        Parameters
        ----------
        cell_specimen_ids: list
            List of cell specimen ids.  Default None.

        Returns
        -------
        np.ndarray: NxWxH array, where N is number of cells
        '''

        roi_masks = self.get_roi_mask(cell_specimen_ids)

        if len(roi_masks) == 0:
            raise IOError("no masks found for given cell specimen ids")

        roi_arr = roi.create_roi_mask_array(roi_masks)

        return roi_arr

    def get_roi_mask(self, cell_specimen_ids=None):
        ''' Returns an array of all the ROI masks

        Parameters
        ----------
        cell specimen IDs: list or array (optional)
            List of cell IDs to return traces for. If this is None (default)
            then all are returned

        Returns
        -------
            List of ROI_Mask objects
        '''

        with h5py.File(self.nwb_file, 'r') as f:
            mask_loc = f['processing'][self.PIPELINE_DATASET][
                'ImageSegmentation']['imaging_plane_1']
            roi_list = f['processing'][self.PIPELINE_DATASET][
                'ImageSegmentation']['imaging_plane_1']['roi_list'].value

            inds = None
            if cell_specimen_ids is None:
                inds = range(self.number_of_cells)
            else:
                inds = self.get_cell_specimen_indices(cell_specimen_ids)

            roi_array = []
            for i in inds:
                v = roi_list[i]
                roi_mask = mask_loc[v]["img_mask"].value
                m = roi.create_roi_mask(roi_mask.shape[1], roi_mask.shape[0],
                                        [0, 0, 0, 0], roi_mask=roi_mask, label=v)
                roi_array.append(m)

        return roi_array

    @property
    def number_of_cells(self):
        '''Number of cells in the experiment'''

        # Replace here is there is a better way to get this info:
        return len(self.get_cell_specimen_ids())


    def get_metadata(self):
        ''' Returns a dictionary of meta data associated with each
        experiment, including Cre line, specimen number,
        visual area imaged, imaging depth

        Returns
        -------
        metadata: dictionary
        '''

        meta = {}

        with h5py.File(self.nwb_file, 'r') as f:
            for memory_key, disk_key in BrainObservatoryNwbDataSet.FILE_METADATA_MAPPING.items():
                try:
                    v = f[disk_key].value

                    # convert numpy strings to python strings
                    if v.dtype.type is np.string_:
                        if len(v.shape) == 0:
                            v = v.decode('UTF-8')
                        elif len(v.shape) == 1:
                            v = [ s.decode('UTF-8') for s in v ]
                        else:
                            raise Exception("Unrecognized metadata formatting for field %s" % disk_key)

                    meta[memory_key] = v
                except KeyError as e:
                    logging.warning("could not find key %s", disk_key)

        # extract cre line from genotype string
        genotype = meta.get('genotype')
        meta['cre_line'] = meta['genotype'].split(';')[0] if genotype else None

        imaging_depth = meta.pop('imaging_depth', None)
        meta['imaging_depth_um'] = int(imaging_depth.split()[0]) if imaging_depth else None

        ophys_experiment_id = meta.get('ophys_experiment_id')
        meta['ophys_experiment_id'] = int(ophys_experiment_id) if ophys_experiment_id else None

        experiment_container_id = meta.get('experiment_container_id')
        meta['experiment_container_id'] = int(experiment_container_id) if experiment_container_id else None

        # convert start time to a date object
        session_start_time = meta.get('session_start_time')
        if isinstance( session_start_time, six.string_types ):
            meta['session_start_time'] = dateutil.parser.parse(session_start_time)

        age = meta.pop('age', None)
        if age:
            # parse the age in days
            m = re.match("(.*?) days", age)
            if m:
                meta['age_days'] = int(m.groups()[0])
            else:
                raise IOError("Could not parse age.")


        # parse the device string (ugly, sorry)
        device_string = meta.pop('device_string', None)
        if device_string:
            m = re.match("(.*?)\.\s(.*?)\sPlease*", device_string)
            if m:
                device, device_name = m.groups()
                meta['device'] = device
                meta['device_name'] = device_name
            else:
                raise IOError("Could not parse device string.")

        # file version
        generated_by = meta.pop('generated_by', None)
        version = generated_by[-1] if generated_by else "0.9"
        meta["pipeline_version"] = version

        return meta

    def get_running_speed(self):
        ''' Returns the mouse running speed in cm/s
        '''
        with h5py.File(self.nwb_file, 'r') as f:
            dx_ds = f['processing'][self.PIPELINE_DATASET][
                'BehavioralTimeSeries']['running_speed']
            dxcm = dx_ds['data'].value
            dxtime = dx_ds['timestamps'].value

        timestamps = self.get_fluorescence_timestamps()

        # v0.9 stored this as an Nx1 array instead of a flat 1-d array
        if len(dxcm.shape) == 2:
            dxcm = dxcm[:, 0]

        dxcm, dxtime = align_running_speed(dxcm, dxtime, timestamps)

        return dxcm, dxtime

    def get_pupil_location(self, as_spherical=True):
        '''Returns the x, y pupil location.

        Parameters
        ----------
        as_spherical : bool
            Whether to return the location as spherical (default) or
            not. If true, the result is altitude and azimuth in
            degrees, otherwise it is x, y in centimeters. (0,0) is
            the center of the monitor.

        Returns
        -------
        (timestamps, location)
            Timestamps is an (Nx1) array of timestamps in seconds.
            Location is an (Nx2) array of spatial location.
        '''
        if as_spherical:
            location_key = "pupil_location_spherical"
        else:
            location_key = "pupil_location"
        try:
            with h5py.File(self.nwb_file, 'r') as f:
                eye_tracking = f['processing'][self.PIPELINE_DATASET][
                    'EyeTracking'][location_key]
                pupil_location = eye_tracking['data'].value
                pupil_times = eye_tracking['timestamps'].value
        except KeyError:
            raise NoEyeTrackingException("No eye tracking for this experiment.")

        return pupil_times, pupil_location

    def get_pupil_size(self):
        '''Returns the pupil area in pixels.

        Returns
        -------
        (timestamps, areas)
            Timestamps is an (Nx1) array of timestamps in seconds.
            Areas is an (Nx1) array of pupil areas in pixels.
        '''
        try:
            with h5py.File(self.nwb_file, 'r') as f:
                pupil_tracking = f['processing'][self.PIPELINE_DATASET][
                    'PupilTracking']['pupil_size']
                pupil_size = pupil_tracking['data'].value
                pupil_times = pupil_tracking['timestamps'].value
        except KeyError:
            raise NoEyeTrackingException("No pupil tracking for this experiment.")

        return pupil_times, pupil_size

    def get_motion_correction(self):
        ''' Returns a Panda DataFrame containing the x- and y- translation of each image used for image alignment
        '''

        motion_correction = None
        with h5py.File(self.nwb_file, 'r') as f:
            pipeline_ds = f['processing'][self.PIPELINE_DATASET]

            # pipeline 0.9 stores this in xy_translations
            # pipeline 1.0 stores this in xy_translation
            for mc_ds_name in self.MOTION_CORRECTION_DATASETS:
                try:
                    mc_ds = pipeline_ds[mc_ds_name]

                    motion_log = mc_ds['data'].value
                    motion_time = mc_ds['timestamps'].value
                    motion_names = mc_ds['feature_description'].value

                    motion_correction = pd.DataFrame(motion_log, columns=motion_names)
                    motion_correction['timestamp'] = motion_time

                    # break out if we found it
                    break
                except KeyError as e:
                    pass

        if motion_correction is None:
            raise KeyError("Could not find motion correction data.")

        return motion_correction

    def save_analysis_dataframes(self, *tables):
        store = pd.HDFStore(self.nwb_file, mode='a')
        for k, v in tables:
            store.put('analysis/%s' % (k), v)
        store.close()

    def save_analysis_arrays(self, *datasets):
        with h5py.File(self.nwb_file, 'a') as f:
            for k, v in datasets:
                if k in f['analysis']:
                    del f['analysis'][k]
                f.create_dataset('analysis/%s' % k, data=v)

    @property
    def stimulus_search(self):

        if self._stimulus_search is None:
            self._stimulus_search = si.StimulusSearch(self)
        return self._stimulus_search

    def get_stimulus(self, frame_ind):

        search_result = self.stimulus_search.search(frame_ind)

        if search_result is None or search_result[2]['stimulus'] == si.SPONTANEOUS_ACTIVITY:
            return None, None

        else:

            curr_stimulus = search_result[2]['stimulus']
            if curr_stimulus in si.LOCALLY_SPARSE_NOISE_STIMULUS_TYPES + si.NATURAL_MOVIE_STIMULUS_TYPES + [si.NATURAL_SCENES]:
                curr_frame = search_result[2]['frame']
                return search_result, self.get_stimulus_template(curr_stimulus)[int(curr_frame), :, :]
            elif curr_stimulus == si.STATIC_GRATINGS or curr_stimulus == si.DRIFTING_GRATINGS:
                return search_result, None

def align_running_speed(dxcm, dxtime, timestamps):
    ''' If running speed timestamps differ from fluorescence
    timestamps, adjust by inserting NaNs to running speed.

    Returns
    -------
    tuple: dxcm, dxtime
    '''
    if dxtime[0] != timestamps[0]:
        adjust = np.where(timestamps == dxtime[0])[0][0]
        dxtime = np.insert(dxtime, 0, timestamps[:adjust])
        dxcm = np.insert(dxcm, 0, np.repeat(np.NaN, adjust))
    adjust = len(timestamps) - len(dxtime)
    if adjust > 0:
        dxtime = np.append(dxtime, timestamps[(-1 * adjust):])
        dxcm = np.append(dxcm, np.repeat(np.NaN, adjust))

    return dxcm, dxtime

#
@deprecated('Use allensdk.brain_observatory.stimulus_info.warp_stimulus_coords instead')
def warp_stimulus_coords(*args, **kwargs):
    return si_warp_stimulus_coords(*args, **kwargs)


@deprecated('Use allensdk.brain_observatory.stimulus_info.make_display_mask instead')
def make_display_mask(*args, **kwargs):
    return si_make_display_mask(*args, **kwargs)



@deprecated('Use allensdk.brain_observatory.stimulus_info.mask_stimulus_template instead')
def mask_stimulus_template(*args, **kwargs):
    return si_mask_stimulus_template(*args, **kwargs)


def _get_abstract_feature_series_stimulus_table(nwb_file, stimulus_name):
    ''' Return the a stimulus table for an abstract feature series.

    Returns
    -------
    stimulus table: pd.DataFrame
    '''


    k = "stimulus/presentation/%s" % stimulus_name

    with h5py.File(nwb_file, 'r') as f:
        if k not in f:
            raise MissingStimulusException(
                "Stimulus not found: %s" % stimulus_name)
        stim_data = f[k + '/data'].value
        features = [ v.decode('UTF-8') for v in f[k + '/features'].value ]
        frame_dur = f[k + '/frame_duration'].value

    stimulus_table = pd.DataFrame(stim_data, columns=features)
    stimulus_table.loc[:, 'start'] = frame_dur[:, 0].astype(int)
    stimulus_table.loc[:, 'end'] = frame_dur[:, 1].astype(int)

    return stimulus_table


def _get_indexed_time_series_stimulus_table(nwb_file, stimulus_name):
    ''' Return the a stimulus table for an indexed time series.

    Returns
    -------
    stimulus table: pd.DataFrame
    '''

    k = "stimulus/presentation/%s" % stimulus_name

    with h5py.File(nwb_file, 'r') as f:
        if k not in f:
            k = "stimulus/presentation/%s" % (stimulus_name + "_stimulus")
            if k not in f:
                raise MissingStimulusException("Stimulus not found: %s" % stimulus_name)
        inds = f[k + '/data'].value
        frame_dur = f[k + '/frame_duration'].value

    stimulus_table = pd.DataFrame(inds, columns=['frame'])
    stimulus_table.loc[:, 'start'] = frame_dur[:, 0].astype(int)
    stimulus_table.loc[:, 'end'] = frame_dur[:, 1].astype(int)

    return stimulus_table

def _get_repeated_indexed_time_series_stimulus_table(nwb_file, stimulus_name):

    stimulus_table = _get_indexed_time_series_stimulus_table(nwb_file, stimulus_name)
    a = stimulus_table.groupby(by='frame')

    # If this ever occurs, the repeat counter cant be trusted!
    assert np.floor(len(stimulus_table))/len(a) == int(len(stimulus_table))/len(a)

    stimulus_table['repeat'] = np.repeat(range(len(stimulus_table)//len(a)), len(a))

    return stimulus_table
