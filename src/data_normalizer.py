from multiprocessing        import Process, Queue, Manager, Value, set_start_method
from typing                 import List, Dict, Tuple, Any
from sklearn.neighbors      import NearestNeighbors
from tabulate               import tabulate

import numpy                as np
import compress_pickle      as pickle
import awkward              as ak
import uproot               as ur
import logging
import tqdm
import gzip

from config_loader          import ConfigLoader
from exceptions             import DataNormalizerException


class DataNormalizer:

    def __init__(
        self, 
        config_loader: ConfigLoader, 
        file_list: list, 
        folder_name: str
    ):
        config = self.config = config_loader
        self.file_list = file_list
        self.folder_name = folder_name
        self.num_files = len(file_list)

        
        if config.CALC_NORMALIZER_STATS is True:
            print(f"Computing normalizer stats for {folder_name} folder")
            self.normalizer_manager(config.NORMALIZER_SAMPLE_SIZE)
        else:
            print(f"Opening normalizer stats for {folder_name} folder")
            with gzip.open(config.OUTPUT_DIR_PATH / 'means.p.gz', 'rb') as means_file:
                self.means_dict = pickle.load(means_file)
            with gzip.open(config.OUTPUT_DIR_PATH / 'stdvs.p.gz', 'rb') as stdvs_file:
                self.stdvs_dict = pickle.load(stdvs_file)


    def normalizer_manager(self, normalizer_sample_size: int):    
        config = self.config

        self.n_calcs = min(normalizer_sample_size, self.num_files)

        with Manager() as manager:
            means = manager.list()
            stdvs = manager.list()

            self.processes = []

            for i in range(self.n_calcs):
                process = Process(
                    target=self._normalizer_process,
                    args=(i, means, stdvs), 
                    daemon=True
                )
                process.start()
                self.processes.append(process)
            

            for process in self.processes:
                process.join()

            means_dict = dict({key:[] for key in config.SCALAR_KEYS})
            stdvs_dict = dict({key:[] for key in config.SCALAR_KEYS})

            for means, stdvs in zip(means, stdvs):
                for key in config.SCALAR_KEYS:
                    means_dict[key].append(means[key])
                    stdvs_dict[key].append(stdvs[key])
            
            self.means_dict = {key: np.mean(value) for key, value in means_dict.items()}
            self.stdvs_dict = {key: np.mean(value) for key, value in stdvs_dict.items()}

            combined_data = []
            for key in self.means_dict.keys():
                combined_data.append([
                    key, 
                    self.means_dict[key], 
                    key, 
                    self.stdvs_dict[key]
                ])

            print(tabulate(
                combined_data, 
                headers=[
                    "Mean Key", 
                    "Mean Value", 
                    "Stdev Key", 
                    "Stdev Value"
                ]
            ))
            
            data_dir_path = config.OUTPUT_DIR_PATH.resolve()
            data_dir_path.mkdir(parents=True, exist_ok=True)

            with gzip.open(data_dir_path / 'means.p.gz', 'wb') as means_file:
                pickle.dump(self.means_dict, means_file)

            with gzip.open(data_dir_path / 'stdvs.p.gz', 'wb') as stdvs_file:
                pickle.dump(self.stdvs_dict, stdvs_file)


    def _normalizer_process(
        self, 
        worker_id: int, 
        means: List[Dict[str, float]], 
        stdevs: List[Dict[str, float]],
    ):
        
        config = self.config

        file_num = worker_id

        file_name = self.file_list[file_num]
        event_tree = ur.open(file_name)['events']
        num_events = event_tree.num_entries
        event_data = event_tree.arrays() 
        
        file_means = {key:[] for key in config.SCALAR_KEYS}
        file_stdvs = {key:[] for key in config.SCALAR_KEYS}
        
        cell_energy = event_data[config.DETECTOR_NAME + ".energy"]
        time = event_data[config.DETECTOR_NAME + ".time"]
        mask = ((cell_energy > config.ENERGY_TH) & 
                (time < config.TIME_TH) & 
                (cell_energy < 1e10))

        if config.INCLUDE_ECAL:
            cell_energy_ecal = event_data[config.DETECTOR_ECAL + ".energy"]
            time_ecal = event_data[config.DETECTOR_ECAL + ".time"]
            mask_ecal = (
                (cell_energy_ecal > config.ENERGY_TH_ECAL) & 
                (time_ecal < config.TIME_TH) & 
                (cell_energy_ecal < 1e10)
            )

        for key in config.SCALAR_KEYS:
            if 'position' in key:
                feature_data = event_data[config.DETECTOR_NAME + key][mask]
                if config.INCLUDE_ECAL:
                    feature_data_ecal = event_data[config.DETECTOR_ECAL + key][mask_ecal]
                    feature_data = ak.concatenate([feature_data, feature_data_ecal])
                file_means[key].append(np.mean(feature_data))
                file_stdvs[key].append(np.std(feature_data))
            elif '.energy' in key:
                if 'Ecal' in key:  
                    feature_data = np.log10(event_data[key][mask_ecal])
                else:
                    feature_data = np.log10(event_data[key][mask])

                file_means[key].append(np.mean(feature_data))
                file_stdvs[key].append(np.std(feature_data))
            else:
                continue

        cluster_sum_E_hcal = ak.sum(cell_energy[mask], axis=-1)
        total_calibration_energy = cluster_sum_E_hcal / config.SAMPLING_FRACTION

        mask = total_calibration_energy > 0.0
        cluster_calib_E = np.log10(total_calibration_energy[mask])

        file_means['cluster_energy'].append(np.mean(cluster_calib_E))
        file_stdvs['cluster_energy'].append(np.std(cluster_calib_E))

        incident_mask = event_data["MCParticles.generatorStatus"] == 1 
        num_particles = len(event_data["MCParticles.PDG"][incident_mask][0]) 

        if num_particles > 1:
            momentum_x = ak.sum(event_data['MCParticles.momentum.x'][incident_mask], 1) 
            momentum_y = ak.sum(event_data['MCParticles.momentum.y'][incident_mask], 1)
            momentum_z = ak.sum(event_data['MCParticles.momentum.z'][incident_mask], 1)
        elif num_particles == 1:
            momentum_x = ak.flatten(event_data['MCParticles.momentum.x'][incident_mask])
            momentum_y = ak.flatten(event_data['MCParticles.momentum.y'][incident_mask])
            momentum_z = ak.flatten(event_data['MCParticles.momentum.z'][incident_mask])

        log_momentum = np.log10(np.sqrt(momentum_x**2 + momentum_y**2 + momentum_z**2))
        momentum = np.sqrt(momentum_x**2 + momentum_y**2 + momentum_z**2)
        theta = np.arccos(momentum_z/momentum)*1000  # in milli-radians
        phi = np.arctan2(momentum_y,momentum_x)*1000

        match config.OUTPUT_DIMENSIONS:
            case 1:
                file_means['momentum'].append(ak.mean(log_momentum))
                file_stdvs['momentum'].append(ak.std(log_momentum))    
            case 2:
                mask_theta = theta < config.THETA_MAX
                theta = theta[mask_theta]
                momentum = momentum[mask_theta]
                file_means['momentum'].append(ak.mean(log_momentum))
                file_stdvs['momentum'].append(ak.std(log_momentum))
                file_means['theta'].append(ak.mean(theta))
                file_stdvs['theta'].append(ak.std(theta))
            case 3:
                mask_theta = theta < config.THETA_MAX
                theta = theta[mask_theta]
                momentum = momentum[mask_theta]
                file_means['momentum'].append(ak.mean(log_momentum))
                file_stdvs['momentum'].append(ak.std(log_momentum))
                file_means['theta'].append(ak.mean(theta))
                file_stdvs['theta'].append(ak.std(theta))
                file_means['phi'].append(ak.mean(phi))
                file_stdvs['phi'].append(ak.std(phi))

        means.append(file_means)
        stdevs.append(file_stdvs)


    def get_normalizer_dicts(self):
        config = self.config
        with gzip.open(config.OUTPUT_DIR_PATH / 'means.p.gz', 'rb') as means_file:
            means_dict = pickle.load(means_file)
        with gzip.open(config.OUTPUT_DIR_PATH / 'stdvs.p.gz', 'rb') as stdvs_file:
            stdvs_dict = pickle.load(stdvs_file)
        
        return means_dict, stdvs_dict