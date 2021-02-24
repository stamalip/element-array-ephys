from datetime import datetime
import numpy as np
import pathlib
from .utils import convert_to_number


class SpikeGLX:

    def __init__(self, root_dir):
        '''
        create neuropixels reader from 'root name' - e.g. the recording:

            /data/rec_1/npx_g0_t0.imec.ap.meta
            /data/rec_1/npx_g0_t0.imec.ap.bin
            /data/rec_1/npx_g0_t0.imec.lf.meta
            /data/rec_1/npx_g0_t0.imec.lf.bin

        would have a 'root name' of:

            /data/rec_1/npx_g0_t0.imec

        only a single recording is read/loaded via the root
        name & associated meta - no interpretation of g0_t0.imec, etc is
        performed at this layer.
        '''
        self._apmeta, self._apdata = None, None
        self._lfmeta, self._lfdata = None, None

        self.root_dir = pathlib.Path(root_dir)

        meta_filepath = next(pathlib.Path(root_dir).glob('*.ap.meta'))
        self.root_name = meta_filepath.name.replace('.ap.meta', '')

    @property
    def apmeta(self):
        if self._apmeta is None:
            self._apmeta = SpikeGLXMeta(self.root_dir / (self.root_name + '.ap.meta'))
        return self._apmeta

    @property
    def apdata(self):
        """
        AP data: (sample x channel)
        Channels' gains (bit_volts) applied - unit: uV
        """
        if self._apdata is None:
            self._apdata = self._read_bin(self.root_dir / (self.root_name + '.ap.bin'))
            self._apdata = self._apdata * self.get_channel_bit_volts('ap')
        return self._apdata

    @property
    def lfmeta(self):
        if self._lfmeta is None:
            self._lfmeta = SpikeGLXMeta(self.root_dir / (self.root_name + '.lf.meta'))
        return self._lfmeta

    @property
    def lfdata(self):
        """
        LFP data: (sample x channel)
        Channels' gains (bit_volts) applied - unit: uV
        """
        if self._lfdata is None:
            self._lfdata = self._read_bin(self.root_dir / (self.root_name + '.lf.bin'))
            self._lfdata = self._lfdata * self.get_channel_bit_volts('lf')
        return self._lfdata

    def get_channel_bit_volts(self, band='ap'):
        """
        Extract the AP and LF channels' int16 to microvolts
        Following the steps specified in: https://billkarsh.github.io/SpikeGLX/Support/SpikeGLX_Datafile_Tools.zip
                dataVolts = dataInt * fI2V / gain
        """
        fI2V = float(self.apmeta.meta['imAiRangeMax']) / 512

        if band == 'ap':
            imroTbl_data = self.apmeta.imroTbl['data']
            imroTbl_idx = 3
        elif band == 'lf':
            imroTbl_data = self.lfmeta.imroTbl['data']
            imroTbl_idx = 4

        # extract channels' gains
        if 'imDatPrb_dock' in self.apmeta.meta:
            # NP 2.0; APGain = 80 for all AP (LF is computed from AP)
            chn_gains = [80] * len(imroTbl_data)
        else:
            # 3A, 3B1, 3B2 (NP 1.0)
            chn_gains = [c[imroTbl_idx] for c in imroTbl_data]

        return fI2V / np.array(chn_gains) * 1e6

    def _read_bin(self, fname):
        nchan = self.apmeta.meta['nSavedChans']
        dtype = np.dtype((np.int16, nchan))
        return np.memmap(fname, dtype, 'r')

    def extract_spike_waveforms(self, spikes, channel, n_wf=500, wf_win=(-32, 32), bit_volts=1):
        """
        :param spikes: spike times (in second) to extract waveforms
        :param channel: channel (name, not indices) to extract waveforms
        :param n_wf: number of spikes per unit to extract the waveforms
        :param wf_win: number of sample pre and post a spike
        :param bit_volts: scalar required to convert int16 values into microvolts (default of 1)
        :return: waveforms (sample x channel x spike)
        """

        data = self.apdata
        channel_idx = [np.where(self.apmeta.recording_channels == chn)[0][0] for chn in channel]

        spikes = np.round(spikes * self.apmeta.meta['imSampRate']).astype(int)  # convert to sample
        # ignore spikes at the beginning or end of raw data
        spikes = spikes[np.logical_and(spikes > -wf_win[0], spikes < data.shape[0] - wf_win[-1])]

        np.random.shuffle(spikes)
        spikes = spikes[:n_wf]
        if len(spikes) > 0:
            # waveform at each spike: (sample x channel x spike)
            spike_wfs = np.dstack([data[int(spk + wf_win[0]):int(spk + wf_win[-1]), channel_idx] for spk in spikes])
            return spike_wfs * bit_volts
        else:  # if no spike found, return NaN of size (sample x channel x 1)
            return np.full((len(range(*wf_win)), len(channel), 1), np.nan)


class SpikeGLXMeta:

    def __init__(self, meta_filepath):
        # a good processing reference: https://github.com/jenniferColonell/Neuropixels_evaluation_tools/blob/master/SGLXMetaToCoords.m

        self.fname = meta_filepath
        self.meta = _read_meta(meta_filepath)

        # Infer npx probe model (e.g. 1.0 (3A, 3B) or 2.0)
        probe_model = self.meta.get('imDatPrb_type', 1)
        if probe_model <= 1:
            if 'typeEnabled' in self.meta:
                self.probe_model = 'neuropixels 1.0 - 3A'
            elif 'typeImEnabled' in self.meta:
                self.probe_model = 'neuropixels 1.0 - 3B'
        elif probe_model == 21:
            self.probe_model = 'neuropixels 2.0 - SS'
        elif probe_model == 24:
            self.probe_model = 'neuropixels 2.0 - MS'
        else:
            self.probe_model = str(probe_model)

        # Get recording time
        self.recording_time = datetime.strptime(self.meta.get('fileCreateTime_original', self.meta['fileCreateTime']),
                                                '%Y-%m-%dT%H:%M:%S')
        self.recording_duration = self.meta['fileTimeSecs']

        # Get probe serial number - 'imProbeSN' for 3A and 'imDatPrb_sn' for 3B
        try:
            self.probe_SN = self.meta.get('imProbeSN', self.meta.get('imDatPrb_sn'))
        except KeyError:
            raise KeyError('Probe Serial Number not found in either "imProbeSN" or "imDatPrb_sn"')

        self.chanmap = self._parse_chanmap(self.meta['~snsChanMap']) if '~snsChanMap' in self.meta else None
        self.shankmap = self._parse_shankmap(self.meta['~snsShankMap']) if '~snsShankMap' in self.meta else None
        self.imroTbl = self._parse_imrotbl(self.meta['~imroTbl']) if '~imroTbl' in self.meta else None

        self.recording_channels = [c[0] for c in self.imroTbl['data']] if self.imroTbl else None

        self._chan_gains = None

    @staticmethod
    def _parse_chanmap(raw):
        '''
        https://github.com/billkarsh/SpikeGLX/blob/master/Markdown/UserManual.md#channel-map
        Parse channel map header structure. Converts:

            '(x,y,z)(c0,x:y)...(cI,x:y),(sy0;x:y)'

        e.g:

            '(384,384,1)(AP0;0:0)...(AP383;383:383)(SY0;768:768)'

        into dict of form:

            {'shape': [x,y,z], 'c0': [x,y], ... }
        '''

        res = {}
        for u in (i.rstrip(')').split(';') for i in raw.split('(') if i != ''):
            if (len(u)) == 1:
                res['shape'] = u[0].split(',')
            else:
                res[u[0]] = u[1].split(':')

        return res

    @staticmethod
    def _parse_shankmap(raw):
        """
        https://github.com/billkarsh/SpikeGLX/blob/master/Markdown/UserManual.md#shank-map
        Parse shank map header structure. Converts:

            '(x,y,z)(a:b:c:d)...(a:b:c:d)'

        e.g:

            '(1,2,480)(0:0:192:1)...(0:1:191:1)'

        into dict of form:

            {'shape': [x,y,z], 'data': [[a,b,c,d],...]}
        """
        res = {'shape': None, 'data': []}

        for u in (i.rstrip(')') for i in raw.split('(') if i != ''):
            if ',' in u:
                res['shape'] = [int(d) for d in u.split(',')]
            else:
                res['data'].append([int(d) for d in u.split(':')])

        return res

    @staticmethod
    def _parse_imrotbl(raw):
        """
        https://github.com/billkarsh/SpikeGLX/blob/master/Markdown/UserManual.md#imro-per-channel-settings
        Parse imro tbl structure. Converts:

            '(X,Y,Z)(A B C D E)...(A B C D E)'

        e.g.:

            '(641251209,3,384)(0 1 0 500 250)...(383 0 0 500 250)'

        into dict of form:

            {'shape': (x,y,z), 'data': []}
        """
        res = {'shape': None, 'data': []}

        for u in (i.rstrip(')') for i in raw.split('(') if i != ''):
            if ',' in u:
                res['shape'] = [int(d) for d in u.split(',')]
            else:
                res['data'].append([int(d) for d in u.split(' ')])

        return res


# ============= HELPER FUNCTIONS =============

def _read_meta(meta_filepath):
    """
    Read metadata in 'k = v' format.

    The fields '~snsChanMap' and '~snsShankMap' are further parsed into
    'snsChanMap' and 'snsShankMap' dictionaries via calls to
    SpikeGLX._parse_chanmap and SpikeGLX._parse_shankmap.
    """

    res = {}
    with open(meta_filepath) as f:
        for l in (l.rstrip() for l in f):
            if '=' in l:
                try:
                    k, v = l.split('=')
                    v = convert_to_number(v)
                    res[k] = v
                except ValueError:
                    pass
    return res
