import copy
import logging
import torch.multiprocessing as mp
from contextlib import ExitStack
from functools import partial
import random
from pathlib import Path
from itertools import chain, groupby

import numpy as np
from sklearn.utils.class_weight import compute_class_weight
from torch.utils.data import ConcatDataset, Dataset, Subset, IterableDataset
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler, SequentialSampler
import torch
from sklearn.model_selection import StratifiedKFold, KFold
from tqdm import tqdm

from farm.data_handler.dataloader import NamedDataLoader
from farm.data_handler.processor import Processor, BertStyleLMProcessor
from farm.data_handler.utils import grouper
from farm.modeling.tokenization import EmbeddingTokenizer
from farm.utils import MLFlowLogger as MlLogger
from farm.utils import log_ascii_workers, calc_chunksize
from farm.utils import get_dict_checksum
from farm.visual.ascii.images import TRACTOR_SMALL


logger = logging.getLogger(__name__)



class DataSilo:
    """ Generates and stores PyTorch DataLoader objects for the train, dev and test datasets.
    Relies upon functionality in the processor to do the conversion of the data. Will also
    calculate and display some statistics.
     """

    def __init__(
        self,
        processor,
        batch_size,
        eval_batch_size=None,
        distributed=False,
        automatic_loading=True,
        max_multiprocessing_chunksize=2000,
        max_processes=128,
        caching=False,
        cache_path=Path("cache/data_silo"),
    ):
        """
        :param processor: A dataset specific Processor object which will turn input (file or dict) into a Pytorch Dataset.
        :type processor: Processor
        :param batch_size: The size of batch that should be returned by the DataLoader for the training set.
        :type batch_size: int
        :param eval_batch_size: The size of batch that should be returned by the DataLoaders for the dev and test set.
        :type eval_batch_size: int
        :param distributed: Set to True if you are running in a distributed evn, e.g. using DistributedDataParallel.
                            The DataSilo will init the DataLoader with a DistributedSampler() to distribute batches.
        :type distributed: bool
        :param automatic_loading: Set to False, if you don't want to automatically load data at initialization.
        :type automatic_loading: bool
        :param max_multiprocessing_chunksize: max possible value for chunksize as calculated by `calc_chunksize()`
            in `farm.utils`. For certain cases like lm_finetuning, a smaller value can be set, as the default chunksize
            values are rather large that might cause memory issues.
        :type max_multiprocessing_chunksize: int
        :param max_processes: the maximum number of processes to spawn in the multiprocessing.Pool used in DataSilo.
                              It can be set to 1 to disable the use of multiprocessing or make debugging easier.
        :type max_processes: int
        :param caching: save the processed datasets on disk to save time/compute if the same train data is used to run
                        multiple experiments. Each cache has a checksum based on the train_filename of the Processor
                        and the batch size.
        :type caching: bool
        :param cache_path: root dir for storing the datasets' cache.
        :type cache_path: Path
        """
        self.distributed = distributed
        self.processor = processor
        self.data = {}
        self.batch_size = batch_size
        self.class_weights = None
        self.max_processes = max_processes
        self.max_multiprocessing_chunksize = max_multiprocessing_chunksize
        self.caching = caching
        self.cache_path = cache_path
        self.tensor_names = None
        if eval_batch_size is None:
            self.eval_batch_size = batch_size
        else:
            self.eval_batch_size = eval_batch_size

        if len(self.processor.tasks) == 0:
            raise Exception("No task initialized. Try initializing the processor with a metric and a label list. "
                            "Alternatively you can add a task using Processor.add_task()")

        if type(self.processor.tokenizer) == EmbeddingTokenizer:
            if max_processes != 1:
                logger.warning("Multiprocessing not efficient for WordEmbedding Tokenizers. Please set max_process \n"
                            "argument in DataSilo to 1.")

        loaded_from_cache = False
        if self.caching:  # Check if DataSets are present in cache
            checksum = self._get_checksum()
            dataset_path = self.cache_path / checksum

            if dataset_path.exists():
                self._load_dataset_from_cache(dataset_path)
                loaded_from_cache = True

        if not loaded_from_cache and automatic_loading:
            # In most cases we want to load all data automatically, but in some cases we rather want to do this
            # later or load from dicts instead of file (https://github.com/deepset-ai/FARM/issues/85)
            self._load_data()

    @classmethod
    def _dataset_from_chunk(cls, chunk, processor):
        """
        Creating a dataset for a chunk (= subset) of dicts. In multiprocessing:
          * we read in all dicts from a file
          * split all dicts into chunks
          * feed *one chunk* to *one process*
          => the *one chunk*  gets converted to *one dataset* (that's what we do here)
          * all datasets get collected and concatenated
        :param chunk: Instead of only having a list of dicts here we also supply an index (ascending int) for each.
            => [(0, dict), (1, dict) ...]
        :type chunk: list of tuples
        :param processor: FARM Processor (e.g. TextClassificationProcessor)
        :return: PyTorch Dataset
        """
        dicts = [d[1] for d in chunk]
        indices = [x[0] for x in chunk]
        dataset, tensor_names, problematic_sample_ids = processor.dataset_from_dicts(dicts=dicts, indices=indices)
        return dataset, tensor_names, problematic_sample_ids

    def _get_dataset(self, filename, dicts=None):
        if not filename and not dicts:
            raise ValueError("You must either supply `filename` or `dicts`")

        # loading dicts from file (default)
        if dicts is None:
            dicts = list(self.processor.file_to_dicts(filename))
            #shuffle list of dicts here if we later want to have a random dev set splitted from train set
            if str(self.processor.train_filename) in str(filename):
                if not self.processor.dev_filename:
                    if self.processor.dev_split > 0.0:
                        random.shuffle(dicts)

        num_dicts = len(dicts)
        multiprocessing_chunk_size, num_cpus_used = calc_chunksize(
            num_dicts=num_dicts,
            max_processes=self.max_processes,
            max_chunksize=self.max_multiprocessing_chunksize,
        )

        with ExitStack() as stack:
            if self.max_processes > 1:  # use multiprocessing only when max_processes > 1
                p = stack.enter_context(mp.Pool(processes=num_cpus_used))

                logger.info(
                    f"Got ya {num_cpus_used} parallel workers to convert {num_dicts} dictionaries "
                    f"to pytorch datasets (chunksize = {multiprocessing_chunk_size})..."
                )
                log_ascii_workers(num_cpus_used, logger)

                results = p.imap(
                    partial(self._dataset_from_chunk, processor=self.processor),
                    grouper(dicts, multiprocessing_chunk_size),
                    chunksize=1,
                )
            else:
                logger.info(
                    f"Multiprocessing disabled, using a single worker to convert {num_dicts}"
                    f" dictionaries to pytorch datasets."
                )

                results = map(partial(self._dataset_from_chunk, processor=self.processor), grouper(dicts, num_dicts))

            datasets = []
            problematic_ids_all = set()

            desc = f"Preprocessing Dataset"
            if filename:
                len_ = len(dicts)
                desc += f" {filename} {len_}"
            with tqdm(total=len(dicts), unit=' Dicts', desc=desc) as pbar:
                for dataset, tensor_names, problematic_samples in results:
                    datasets.append(dataset)
                    # update progress bar (last step can have less dicts than actual chunk_size)
                    pbar.update(min(multiprocessing_chunk_size, pbar.total-pbar.n))
                    #pbar.update(len(datasets))
                    problematic_ids_all.update(problematic_samples)

            self.processor.log_problematic(problematic_ids_all)
            # _dataset_from_chunk can return a None in cases where downsampling has occurred
            datasets = [d for d in datasets if d]
            concat_datasets = ConcatDataset(datasets)
            return concat_datasets, tensor_names

    def _load_data(self, train_dicts=None, dev_dicts=None, test_dicts=None):
        """
        Loading the train, dev and test datasets either from files (default) or from supplied dicts.
        The processor is called to handle the full conversion from "raw data" to a Pytorch Dataset.
        The resulting datasets are loaded into DataSilo.data

        :param train_dicts: (Optional) dicts containing examples for training.
        :param dev_dicts: (Optional) dicts containing examples for dev.
        :param test_dicts: (Optional) dicts containing examples for test.
        :return: None
        """

        logger.info("\nLoading data into the data silo ..."
                    "{}".format(TRACTOR_SMALL))
        # train data
        logger.info("LOADING TRAIN DATA")
        logger.info("==================")
        if train_dicts:
            # either from supplied dicts
            logger.info("Loading train set from supplied dicts ")
            self.data["train"], self.tensor_names = self._get_dataset(filename=None, dicts=train_dicts)
        elif self.processor.train_filename:
            # or from a file (default)
            train_file = self.processor.data_dir / self.processor.train_filename
            logger.info("Loading train set from: {} ".format(train_file))
            self.data["train"], self.tensor_names = self._get_dataset(train_file)
        else:
            logger.info("No train set is being loaded")
            self.data["train"] = None

        # dev data
        logger.info("")
        logger.info("LOADING DEV DATA")
        logger.info("=================")
        if dev_dicts:
            # either from supplied dicts
            logger.info("Loading train set from supplied dicts ")
            self.data["dev"], self.tensor_names = self._get_dataset(filename=None, dicts=dev_dicts)
        elif self.processor.dev_filename:
            # or from file (default)
            dev_file = self.processor.data_dir / self.processor.dev_filename
            logger.info("Loading dev set from: {}".format(dev_file))
            self.data["dev"], _ = self._get_dataset(dev_file)
        elif self.processor.dev_split > 0.0:
            # or split it apart from train set
            logger.info("Loading dev set as a slice of train set")
            self._create_dev_from_train()
        else:
            logger.info("No dev set is being loaded")
            self.data["dev"] = None

        logger.info("")
        logger.info("LOADING TEST DATA")
        logger.info("=================")
        # test data
        if test_dicts:
            # either from supplied dicts
            logger.info("Loading train set from supplied dicts ")
            self.data["test"], self.tensor_names = self._get_dataset(filename=None, dicts=test_dicts)
        elif self.processor.test_filename:
            # or from file (default)
            test_file = self.processor.data_dir / self.processor.test_filename
            logger.info("Loading test set from: {}".format(test_file))
            if self.tensor_names:
                self.data["test"], _ = self._get_dataset(test_file)
            else:
                self.data["test"], self.tensor_names = self._get_dataset(test_file)
        else:
            logger.info("No test set is being loaded")
            self.data["test"] = None

        if self.caching:
            self._save_dataset_to_cache()

        # derive stats and meta data
        self._calculate_statistics()
        # self.calculate_class_weights()

        self._initialize_data_loaders()

    def _load_dataset_from_cache(self, cache_dir):
        """
        Load serialized dataset from a cache.
        """
        logger.info(f"Loading datasets from cache at {cache_dir}")
        self.data["train"] = torch.load(cache_dir / "train_dataset")

        dev_dataset_path = cache_dir / "dev_dataset"
        if dev_dataset_path.exists():
            self.data["dev"] = torch.load(dev_dataset_path)
        else:
            self.data["dev"] = None

        test_dataset_path = cache_dir / "test_dataset"
        if test_dataset_path.exists():
            self.data["test"] = torch.load(test_dataset_path)
        else:
            self.data["test"] = None

        self.tensor_names = torch.load(cache_dir / "tensor_names")

        # derive stats and meta data
        self._calculate_statistics()
        # self.calculate_class_weights()

        self._initialize_data_loaders()

    def _get_checksum(self):
        """
        Get checksum based on a dict to ensure validity of cached DataSilo
        """
        # keys in the dict identifies uniqueness for a given DataSilo.
        payload_dict = {
            "train_filename": str(Path(self.processor.train_filename).absolute()),
            "data_dir": str(self.processor.data_dir.absolute()),
            "max_seq_len": self.processor.max_seq_len,
            "dev_split": self.processor.dev_split,
            "tasks": self.processor.tasks
        }
        checksum = get_dict_checksum(payload_dict)
        return checksum

    def _save_dataset_to_cache(self):
        """
        Serialize and save dataset to a cache.
        """
        checksum = self._get_checksum()

        cache_dir = self.cache_path / checksum
        cache_dir.mkdir(parents=True, exist_ok=True)

        torch.save(self.data["train"], cache_dir / "train_dataset")

        if self.data["dev"]:
            torch.save(self.data["dev"], cache_dir / "dev_dataset")

        if self.data["test"]:
            torch.save(self.data["test"], cache_dir / "test_dataset")

        torch.save(self.tensor_names, cache_dir / "tensor_names")
        logger.info(f"Cached the datasets at {cache_dir}")

    def _initialize_data_loaders(self):
        """ Initializing train, dev and test data loaders for the already loaded datasets """

        if self.data["train"] is not None:
            if self.distributed:
                sampler_train = DistributedSampler(self.data["train"])
            else:
                sampler_train = RandomSampler(self.data["train"])

            data_loader_train = NamedDataLoader(
                dataset=self.data["train"],
                sampler=sampler_train,
                batch_size=self.batch_size,
                tensor_names=self.tensor_names,
            )
        else:
            data_loader_train = None

        if self.data["dev"] is not None:
            data_loader_dev = NamedDataLoader(
                dataset=self.data["dev"],
                sampler=SequentialSampler(self.data["dev"]),
                batch_size=self.eval_batch_size,
                tensor_names=self.tensor_names,
            )
        else:
            data_loader_dev = None

        if self.data["test"] is not None:
            data_loader_test = NamedDataLoader(
                dataset=self.data["test"],
                sampler=SequentialSampler(self.data["test"]),
                batch_size=self.eval_batch_size,
                tensor_names=self.tensor_names,
            )
        else:
            data_loader_test = None

        self.loaders = {
            "train": data_loader_train,
            "dev": data_loader_dev,
            "test": data_loader_test,
        }

    def _create_dev_from_train(self):
        """ Split a dev set apart from the train dataset """
        n_dev = int(self.processor.dev_split * len(self.data["train"]))
        n_train = len(self.data["train"]) - n_dev

        train_dataset, dev_dataset = self.random_split_ConcatDataset(self.data["train"], lengths=[n_train, n_dev])
        self.data["train"] = train_dataset
        if(len(dev_dataset) > 0):
            self.data["dev"] = dev_dataset
        else:
            logger.warning("No dev set created. Please adjust the dev_split parameter.")

        logger.info(
            f"Took {len(dev_dataset)} samples out of train set to create dev set (dev split is roughly {self.processor.dev_split})"
        )

    def random_split_ConcatDataset(self, ds, lengths):
        """
        Roughly split a Concatdataset into non-overlapping new datasets of given lengths.
        Samples inside Concatdataset should already be shuffled

        :param ds: Dataset to be split
        :type ds: Dataset
        :param lengths: lengths of splits to be produced
        :type lengths: list
        """
        if sum(lengths) != len(ds):
            raise ValueError("Sum of input lengths does not equal the length of the input dataset!")

        try:
            idx_dataset = np.where(np.array(ds.cumulative_sizes) > lengths[0])[0][0]
        except IndexError:
            raise Exception("All dataset chunks are being assigned to train set leaving no samples for dev set. "
                            "Either consider increasing dev_split or setting it to 0.0\n"
                            f"Cumulative chunk sizes: {ds.cumulative_sizes}\n"
                            f"train/dev split: {lengths}")

        assert idx_dataset >= 1, "Dev_split ratio is too large, there is no data in train set. " \
                             f"Please lower dev_split = {self.processor.dev_split}"

        train = ConcatDataset(ds.datasets[:idx_dataset])
        test = ConcatDataset(ds.datasets[idx_dataset:])
        return train, test

    def _calculate_statistics(self):
        """ Calculate and log simple summary statistics of the datasets """
        logger.info("")
        logger.info("DATASETS SUMMARY")
        logger.info("================")

        self.counts = {}
        clipped = -1
        ave_len = -1

        if self.data["train"]:
            self.counts["train"] = len(self.data["train"])
            if "input_ids" in self.tensor_names:
                clipped, ave_len, seq_lens, max_seq_len = self._calc_length_stats_single_encoder()
            elif "query_input_ids" in self.tensor_names and "passage_input_ids" in self.tensor_names:
                clipped, ave_len, seq_lens, max_seq_len = self._calc_length_stats_biencoder()
            else:
                logger.warning(f"Could not compute length statistics because 'input_ids' or 'query_input_ids' and 'passage_input_ids' are missing.")
                clipped = -1
                ave_len = -1
        else:
            self.counts["train"] = 0

        if self.data["dev"]:
            self.counts["dev"] = len(self.data["dev"])
        else:
            self.counts["dev"] = 0

        if self.data["test"]:
            self.counts["test"] = len(self.data["test"])
        else:
            self.counts["test"] = 0


        logger.info("Examples in train: {}".format(self.counts["train"]))
        logger.info("Examples in dev  : {}".format(self.counts["dev"]))
        logger.info("Examples in test : {}".format(self.counts["test"]))
        logger.info("")
        if self.data["train"]:
            if "input_ids" in self.tensor_names:
                logger.info("Longest sequence length observed after clipping:     {}".format(max(seq_lens)))
                logger.info("Average sequence length after clipping: {}".format(ave_len))
                logger.info("Proportion clipped:      {}".format(clipped))
                if clipped > 0.5:
                    logger.info("[Farmer's Tip] {}% of your samples got cut down to {} tokens. "
                                "Consider increasing max_seq_len. "
                                "This will lead to higher memory consumption but is likely to "
                                "improve your model performance".format(round(clipped * 100, 1), max_seq_len))
            elif "query_input_ids" in self.tensor_names and "passage_input_ids" in self.tensor_names:
                logger.info("Longest query length observed after clipping: {}   - for max_query_len: {}".format(max(seq_lens[0]),max_seq_len[0]))
                logger.info("Average query length after clipping:          {}".format(ave_len[0]))
                logger.info("Proportion queries clipped:                   {}".format(clipped[0]))
                logger.info("")
                logger.info("Longest passage length observed after clipping: {}   - for max_passage_len: {}".format(max(seq_lens[1]),max_seq_len[1]))
                logger.info("Average passage length after clipping:          {}".format(ave_len[1]))
                logger.info("Proportion passages clipped:                    {}".format(clipped[1]))

        MlLogger.log_params(
            {
                "n_samples_train": self.counts["train"],
                "n_samples_dev": self.counts["dev"],
                "n_samples_test": self.counts["test"],
                "batch_size": self.batch_size,
                "ave_seq_len": ave_len,
                "clipped": clipped,
            }
        )
        
    def _calc_length_stats_single_encoder(self):
        seq_lens = []
        for dataset in self.data["train"].datasets:
            train_input_numpy = dataset[:][self.tensor_names.index("input_ids")].numpy()
            seq_lens.extend(np.sum(train_input_numpy != self.processor.tokenizer.pad_token_id, axis=1))
        max_seq_len = dataset[:][self.tensor_names.index("input_ids")].shape[1]
        clipped = np.mean(np.array(seq_lens) == max_seq_len) if seq_lens else 0
        ave_len = np.mean(seq_lens) if seq_lens else 0
        return clipped, ave_len, seq_lens, max_seq_len

    def _calc_length_stats_biencoder(self):
        seq_lens = [[], []]
        for dataset in self.data["train"].datasets:
            query_input_numpy = dataset[:][self.tensor_names.index("query_input_ids")].numpy()
            num_passages = dataset[:][self.tensor_names.index("passage_input_ids")].shape[1]
            bs = dataset[:][self.tensor_names.index("passage_input_ids")].shape[0]
            passage_input_numpy = dataset[:][self.tensor_names.index("passage_input_ids")].numpy().reshape((bs,-1), order='C')
            qlen = np.sum(query_input_numpy != self.processor.query_tokenizer.pad_token_id, axis=1)
            plen = np.sum(passage_input_numpy != self.processor.passage_tokenizer.pad_token_id, axis=1) / num_passages
            seq_lens[0].extend(qlen)
            seq_lens[1].extend(plen)
        q_max_seq_len = dataset[:][self.tensor_names.index("query_input_ids")].shape[1]
        p_max_seq_len = dataset[:][self.tensor_names.index("passage_input_ids")].shape[2]
        clipped_q = np.mean(np.array(seq_lens[0]) == q_max_seq_len) if seq_lens[0] else 0
        ave_len_q = np.mean(seq_lens[0]) if seq_lens[0] else 0
        clipped_p = np.mean(np.array(seq_lens[1]) == p_max_seq_len) if seq_lens[1] else 0
        ave_len_p = np.mean(seq_lens[1]) if seq_lens[1] else 0
        clipped = [clipped_q, clipped_p]
        ave_len = [ave_len_q, ave_len_p]
        max_seq_len = [q_max_seq_len, p_max_seq_len]
        return clipped, ave_len, seq_lens, max_seq_len

    def calculate_class_weights(self, task_name, source="train"):
        """ For imbalanced datasets, we can calculate class weights that can be used later in the
        loss function of the prediction head to upweight the loss of minorities.

        :param task_name: name of the task as used in the processor
        :type task_name: str
        """
        
        tensor_name = self.processor.tasks[task_name]["label_tensor_name"]
        label_list = self.processor.tasks[task_name]["label_list"]
        tensor_idx = list(self.tensor_names).index(tensor_name)
        # we need at least ONE observation for each label to avoid division by zero in compute_class_weights.
        observed_labels = copy.deepcopy(label_list)
        if source == "all":
            datasets = self.data.values()
        elif source == "train":
            datasets = [self.data["train"]]
        else:
            raise Exception("source argument expects one of [\"train\", \"all\"]")
        for dataset in datasets:
            if "multilabel" in self.processor.tasks[task_name]["task_type"]:
                for x in dataset:
                    observed_labels += [label_list[label_id] for label_id in (x[tensor_idx] == 1).nonzero()]
            else:
                observed_labels += [label_list[x[tensor_idx].item()] for x in dataset]

        #TODO scale e.g. via logarithm to avoid crazy spikes for rare classes
        class_weights = compute_class_weight("balanced", classes=np.asarray(label_list), y=observed_labels)

        # conversion necessary to have class weights of same type as model weights
        class_weights = class_weights.astype(np.float32)
        return class_weights

    def get_data_loader(self, dataset_name):
        return self.loaders[dataset_name]

    def n_samples(self, dataset_name):
        """
        Returns the number of samples in a given dataset.

        :param dataset_name: Choose from train, dev or test
        :type dataset_name: str
        """
        return self.counts[dataset_name]


class StreamingDataSilo:
    """
    Streaming Data Silo loads and preprocesses datasets in parallel to the model training.

    The samples are lazily created from the input file and batches are yielded on-the-fly when required during training.
    This is useful if you:
    - work with large datasets that don't fit in memory
    - want to save time (by not preprocessing the entire dataset before starting training)

    For optimal training performance and efficient utilization of shiny GPUs, the pipeline always keeps a few
    pre-computed batches ready to avoid any waiting time when a batch is requested during training.

    To parallelize the creation of batches, PyTorch DataLoader provide an option to use
    multiple workers that utilize the available CPU cores and ensure enough pre-computed batches.
    """

    def __init__(self, processor, batch_size, distributed=False, dataloader_workers=8):
        """
        :param processor: A dataset specific Processor object which will turn input file into a Pytorch Dataset.
        :type processor: Processor
        :param batch_size: The size of batch to use for model training.
        :type batch_size: int
        :param dataloader_workers: number of workers for PyTorch DataLoader to create batches in parallel
        :type dataloader_workers: int
        """

        self.processor = processor
        self.batch_size = batch_size
        self.dataloader_workers = dataloader_workers
        self.distributed = distributed

    def get_data_loader(self, dataset_name):
        """
        Returns a new instance of dataloader for the given dataset.

        The dataloader lazily yields from Iterable DataSets. After a complete iteration
        over the input data, the generators gets exhausted. So, for instance, in the 
        case of model training, a new train dataloader must be used for each train epoch.

        :param dataset_name: 'train', 'dev', or 'test' set.
        :type dataset_name: str
        """
        filename = None
        if dataset_name == "train":
            filename = self.processor.train_filename
        elif dataset_name == "dev":
            if self.processor.dev_split > 0.0:
                raise NotImplemented(
                            "StreamingDataSilo does not have dev_split implemented. "
                            "To use dev data, supply a dev filename when creating the Processor."
                )
            elif self.processor.dev_filename:
                filename = self.processor.dev_filename
        elif dataset_name == "test":
            if self.processor.test_filename:
                filename = self.processor.test_filename

        if not filename:
            return None

        #  Batching:
        #
        #  The model Trainer is passed a PyTorch DataLoader instance that yields dataset batches for training.
        #
        #  By default, the PyTorch DataLoader prefetch (2 * num_workers) samples. However, given the higher
        #  batch sizes(usually >64) for model training, the default prefetch is not sufficient to keep the
        #  model Training saturated with datasets.
        #
        #  As a workaround, we yield batches of samples instead of yielding individual samples. The DataLoader
        #  can then prefetch (2 * num_workers) number of batches of samples.
        #
        #  Since the batching is now handled within _StreamingDataSet, we disable the batching on DataLoader side
        #  by initializing the data loader with batch_size as 1.

        if isinstance(filename, Path) and filename.is_dir():
            filepath = filename
        else:
            filepath = self.processor.data_dir / filename

        data_set = _StreamingDataSet(
            processor=self.processor,
            filepath=filepath,
            batch_size=self.batch_size,
            dataloader_workers=self.dataloader_workers,
            distributed = self.distributed
        )

        data_loader = NamedDataLoader(
            dataset=data_set, batch_size=1, num_workers=self.dataloader_workers, pin_memory=True
        )
        return data_loader


class _StreamingDataSet(IterableDataset):
    def __init__(self, processor, filepath, batch_size, dataloader_workers, distributed=False, n_samples=None):
        """
        :param processor: A dataset specific Processor object which will turn input file into a Pytorch Dataset.
        :type processor: Processor
        :param batch_size: The size of batch that should be returned by the DataLoaders.
        :type batch_size: int
        :param filepath: input filename to load the dataset from
        :type filepath: Path
        :param dataloader_workers: number of workers for PyTorch Dataloader
        :type dataloader_workers: int
        """

        self.batch_size = batch_size
        self.processor = processor
        self.filepath = filepath
        self.dataloader_workers = dataloader_workers
        self.distributed = distributed

        # calculate or estimate number of samples so that the data loader can derive number of training steps
        if filepath.is_file():
            files = [filepath]
        else:
            files = [file for file in filepath.iterdir()]

        if n_samples:
            self.n_samples = n_samples
        else:
            try:
                self.n_samples = self.processor.estimate_n_samples(files[0]) * len(files)
            except AttributeError:
                AttributeError(f"Could not estimate n_samples for {self.processor.__class__.__name__} in StreamingDataSilo. "
                                    f"Make sure that your Processor has `estimate_n_samples()` implemented")
        logger.info(f"Found data for {self.n_samples} samples")
        self.shuffle_files(files)

        dicts_from_files = [processor.file_to_dicts(file) for file in files]
        self.file_to_dicts_generator = chain(*dicts_from_files)

        if self.distributed:
            self.rank = torch.distributed.get_rank()
            self.world_size = torch.distributed.get_world_size()

    def __len__(self):
        if self.distributed:
            # only a heuristic as we don't necessarily split samples equally across ranks
            len = self.n_samples // self.world_size
        else:
            len = self.n_samples
        return len

    def __iter__(self):
        #  With IterableDataset, the same __iter__ is copied over to the multiple workers of
        #  a Dataloader. Hence, we need to configure the __iter__ to not yield duplicated data
        #  when more than 1 workers are used.
        #
        #  To avoid duplicates, we need to split the input dicts between the workers.
        #  The grouper() converts a dict generator given as input and yields only the
        #  dicts that are to be processed by the given worker_id.
        #
        #  For instance, consider input as [dictA, dictB, dictC, ...], then the grouper
        #  (with n=2) will return, [[dictA, dictB], [dictE, dictF] ...] for worker 1 and
        #  [[dictC, dictD], [dictG, dictH] ...] for worker 2.

        worker_info = torch.utils.data.get_worker_info()
        if self.distributed:
            worker_id = self.rank * worker_info.num_workers + worker_info.id
            total_workers = self.world_size * worker_info.num_workers
        else:
            worker_id = worker_info.id
            total_workers = self.dataloader_workers

        dicts = grouper(self.file_to_dicts_generator, n=10, worker_id=worker_id, total_workers=total_workers)
        results = map(self._dataset_from_chunk, dicts)

        batch = []
        for datasets, tensor_names in results:
            if not datasets:
                continue
            self.tensor_names = tensor_names
            for ds in datasets:
                batch.append(ds)
                if len(batch) == self.batch_size:
                    yield batch
                    batch = []
        if batch:
            yield batch

    def _dataset_from_chunk(self, chunk):
        """
        Creating a dataset for a chunk (= subset) of dicts.
        :param chunk: Instead of only having a list of dicts here we also supply an index (ascending int) for each.
            => [(0, dict), (1, dict) ...]
        :type chunk: list of tuples
        :return: PyTorch Dataset
        """
        dicts = [d[1] for d in chunk]
        # need at least 2 documents to sample random sentences from
        if len(dicts) < 2 and type(self.processor) == BertStyleLMProcessor:
            logger.info("Skipping a dict chunk as it contains less than 2 documents ...")
            return None, None
        indices = [x[0] for x in chunk]
        datasets, tensor_names, _ = self.processor.dataset_from_dicts(dicts=dicts, indices=indices)
        return datasets, tensor_names

    def shuffle_files(self, files, seed=None):
        if not seed:
            seed = random.randrange(100)
        random.seed(seed)
        random.shuffle(files)
        return files


class DataSiloForCrossVal:
    """
    Perform cross validation or nested cross validation.

    For performing cross validation or nested cross validation, we really want to combine all the
    instances from all the sets or just some of the sets, then create a different data silo
    instance for each fold or nested fold.
    Calling DataSiloForCrossVal.make() creates a list of DataSiloForCrossVal instances - one for each fold.
    """

    def __init__(self, origsilo, trainset, devset, testset):
        self.tensor_names = origsilo.tensor_names
        self.data = {"train": trainset, "dev": devset, "test": testset}
        self.processor = origsilo.processor
        self.batch_size = origsilo.batch_size
        # should not be necessary, xval makes no sense with huge data
        # sampler_train = DistributedSampler(self.data["train"])
        sampler_train = RandomSampler(trainset)

        self.data_loader_train = NamedDataLoader(
            dataset=trainset,
            sampler=sampler_train,
            batch_size=self.batch_size,
            tensor_names=self.tensor_names,
        )
        self.data_loader_dev = NamedDataLoader(
            dataset=devset,
            sampler=SequentialSampler(devset),
            batch_size=self.batch_size,
            tensor_names=self.tensor_names,
        )
        self.data_loader_test = NamedDataLoader(
            dataset=testset,
            sampler=SequentialSampler(testset),
            batch_size=self.batch_size,
            tensor_names=self.tensor_names,
        )
        self.loaders = {
            "train": self.data_loader_train,
            "dev": self.data_loader_dev,
            "test": self.data_loader_test,
        }

    def get_data_loader(self, which):
        return self.loaders[which]

    @classmethod
    def make(cls, datasilo, sets=["train", "dev", "test"], n_splits=5, shuffle=True, random_state=None,
             stratified=True, n_neg_answers_per_question=1, n_inner_splits=None):
        """
        Create number of folds data-silo-like objects which can be used for training from the
        original data silo passed on.

        :param datasilo: the data silo that contains the original data
        :type datasilo: DataSilo
        :param sets: which sets to use to create the xval folds (strings)
        :type sets: list
        :param n_splits: number of folds to create
        :type n_splits: int
        :param shuffle: shuffle each class' samples before splitting
        :type shuffle: bool
        :param random_state: random state for shuffling
        :type random_state: int
        :param stratified: If class stratification should be done.
            It is never done with question answering.
        :type stratified: bool
        :param n_neg_answers_per_question: number of negative answers per question to include for training
        :type n_neg_answers_per_question: int
        :param n_inner_splits: Number of inner splits of a nested cross validation.
            Default is ``None`` which means to do a normal (not nested) cross validation.
            If at least 2 is given a nested cross validation is done. In that case the ``n_splits``
            parameter is the number of outer splits.
            The outer cross validation splits the data into a test set and a rest set.
            The inner cross validation splits the rest data into a train set and a dev set.
            The advantage of a nested cross validation is that it is doing the inner split
            not just by random but in a more systematic way. When doing model evaluation
            this also reduces the variance. This is because you train on more different
            iterations with more different data constellations.
        :type n_inner_splits: int
        """
        # check n_inner_splits param
        if (n_inner_splits is not None) and (not n_inner_splits >= 2):
            raise ValueError("'n_inner_splits' must be at least 2!")

        if "question_answering" in datasilo.processor.tasks and n_inner_splits is None:
            return cls._make_question_answering(
                datasilo, sets, n_splits, shuffle, random_state, n_neg_answers_per_question
            )
        elif "question_answering" in datasilo.processor.tasks and n_inner_splits is not None:
            raise NotImplementedError()
        elif n_inner_splits is None:
            return cls._make(
                datasilo, sets, n_splits, shuffle, random_state, stratified
            )
        elif n_inner_splits is not None:
            return cls._make_nested(
                datasilo, sets, n_splits, shuffle, random_state, stratified,
                n_inner_splits
            )
        else:
            raise RuntimeError("Cross validation can not be done under these conditions!")


    @classmethod
    def _make_question_answering(cls, datasilo, sets=["train", "dev", "test"], n_splits=5, shuffle=True,
                                 random_state=None, n_neg_answers_per_question=1):
        """
        Create number of folds data-silo-like objects which can be used for training from the
        original data silo passed on. This function takes into account the characteristics of the
        data for question-answering-

        :param datasilo: the data silo that contains the original data
        :type datasilo: DataSilo
        :param sets: which sets to use to create the xval folds (strings)
        :type sets: list
        :param n_splits: number of folds to create
        :type n_splits: int
        :param shuffle: shuffle each class' samples before splitting
        :type shuffle: bool
        :param random_state: random state for shuffling
        :type random_state: int
        :param n_neg_answers_per_question: number of negative answers per question to include for training
        :type n_neg_answers_per_question: int
        """
        assert "id" in datasilo.tensor_names, f"Expected tensor 'id' in tensor names, found {datasilo.tensor_names}"
        assert "labels" in datasilo.tensor_names, f"Expected tensor 'labels' in tensor names, found {datasilo.tensor_names}"

        id_index = datasilo.tensor_names.index("id")
        label_index = datasilo.tensor_names.index("labels")

        sets_to_concat = []
        for setname in sets:
            if datasilo.data[setname]:
                sets_to_concat.extend(datasilo.data[setname])
        all_data = ConcatDataset(sets_to_concat)

        documents = []
        keyfunc = lambda x: x[id_index][0]
        all_data = sorted(all_data.datasets, key=keyfunc)
        for key, document in groupby(all_data, key=keyfunc):
            documents.append(list(document))

        xval_split = cls._split_for_qa(documents = documents,
                                       id_index=id_index,
                                       n_splits=n_splits,
                                       shuffle=shuffle,
                                       random_state=random_state,
                                       )
        silos = []

        for train_set, test_set in xval_split:
            # Each training set is further divided into actual train and dev set
            if datasilo.processor.dev_split > 0:
                dev_split = datasilo.processor.dev_split
                n_dev = int(np.ceil(dev_split * len(train_set)))
                assert n_dev > 0, f"dev split of {dev_split} is not large enough to split away a development set"
                n_actual_train = len(train_set) - n_dev
                actual_train_set = train_set[:n_actual_train]
                dev_set = train_set[n_actual_train:]
                ds_dev = [sample for document in dev_set for sample in document]
            else:
                ds_dev = None
                actual_train_set = train_set

            train_samples = []
            for doc in actual_train_set:
                keyfunc = lambda x: x[id_index][1]
                doc = sorted(doc, key=keyfunc)
                for key, question in groupby(doc, key=keyfunc):
                    # add all available answrs to train set
                    sample_list = list(question)
                    neg_answer_idx = []
                    for index, sample in enumerate(sample_list):
                        if sample[label_index][0][0] or sample[label_index][0][1]:
                            train_samples.append(sample)
                        else:
                            neg_answer_idx.append(index)
                    # add random n_neg_answers_per_question samples to train set
                    if len(neg_answer_idx) <= n_neg_answers_per_question:
                        train_samples.extend([sample_list[idx] for idx in neg_answer_idx])
                    else:
                        neg_answer_idx = random.sample(neg_answer_idx, n_neg_answers_per_question)
                        train_samples.extend([sample_list[idx] for idx in neg_answer_idx])

            ds_train = train_samples
            ds_test = [sample for document in test_set for sample in document]
            silos.append(DataSiloForCrossVal(datasilo, ds_train, ds_dev, ds_test))
        return silos

    @staticmethod
    def _make(datasilo, sets=["train", "dev", "test"], n_splits=5, shuffle=True,
              random_state=None, stratified=True):
        """
        Create number of folds data-silo-like objects which can be used for training from the
        original data silo passed on.

        :param datasilo: the data silo that contains the original data
        :param sets: which sets to use to create the xval folds
        :param n_splits: number of folds to create
        :param shuffle: shuffle each class' samples before splitting
        :param random_state: random state for shuffling
        :param stratified: if class stratification should be done
        """
        setstoconcat = [datasilo.data[setname] for setname in sets]
        ds_all = ConcatDataset(setstoconcat)
        idxs = list(range(len(ds_all)))
        dev_split = datasilo.processor.dev_split
        if stratified:
            # get all the labels for stratification
            ytensors = [t[3][0] for t in ds_all]
            Y = torch.stack(ytensors)
            xval = StratifiedKFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
            xval_split = xval.split(idxs,Y)
        else:
            xval = KFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
            xval_split = xval.split(idxs)
        # for each fold create a DataSilo4Xval instance, where the training set is further
        # divided into actual train and dev set
        silos = []
        for train_idx, test_idx in xval_split:
            n_dev = int(dev_split * len(train_idx))
            n_actual_train = len(train_idx) - n_dev
            # TODO: this split into actual train and test set could/should also be stratified, for now
            # we just do this by taking the first/last indices from the train set (which should be
            # shuffled by default)
            actual_train_idx = train_idx[:n_actual_train]
            dev_idx = train_idx[n_actual_train:]
            # create the actual datasets
            ds_train = Subset(ds_all, actual_train_idx)
            ds_dev = Subset(ds_all, dev_idx)
            ds_test = Subset(ds_all, test_idx)
            silos.append(DataSiloForCrossVal(datasilo, ds_train, ds_dev, ds_test))
        return silos

    @staticmethod
    def _split_for_qa(documents, id_index, n_splits=5, shuffle=True, random_state=None):
        keyfunc = lambda x: x[id_index][1]
        if shuffle:
            random.shuffle(documents, random_state)

        questions_per_doc = []
        for doc in documents:
            # group samples in current doc by question id
            doc = sorted(doc, key=keyfunc)
            questions = list(groupby(doc, key=keyfunc))
            questions_per_doc.append(len(questions))

        # split documents into n_splits splits with approximately same number of questions per split
        questions_per_doc = np.array(questions_per_doc)
        accumulated_questions_per_doc = questions_per_doc.cumsum()
        questions_per_fold = accumulated_questions_per_doc[-1] // n_splits
        accumulated_questions_per_fold = np.array(range(1, n_splits)) * questions_per_fold
        if accumulated_questions_per_fold[0] < accumulated_questions_per_doc[0]:
            accumulated_questions_per_fold[0] = accumulated_questions_per_doc[0] + 1
        indices_to_split_at = np.searchsorted(accumulated_questions_per_doc, accumulated_questions_per_fold, side="right")
        splits = np.split(documents, indices_to_split_at)

        for split in splits:
            assert len(split) > 0

        for idx, split in enumerate(splits):
            current_test_set = split
            current_train_set = np.hstack(np.delete(splits, idx, axis=0))

            yield current_train_set, current_test_set

    @staticmethod
    def _make_nested(datasilo, sets=["train", "dev", "test"],
                     n_splits=5, shuffle=True, random_state=None,
                     stratified=True, n_inner_splits=5):
        setstoconcat = [datasilo.data[setname] for setname in sets]
        ds_all = ConcatDataset(setstoconcat)
        idxs = list(range(len(ds_all)))

        silos = []

        # outer cross validation where we split all data to test and rest
        if stratified:
            # get all the labels for stratification
            ytensors = [t[3][0] for t in ds_all]
            y = torch.stack(ytensors)
            outer_cv = StratifiedKFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
            outer_split = outer_cv.split(idxs, y)
        else:
            outer_cv = KFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
            outer_split = outer_cv.split(idxs)
        for idxs_rest, idxs_test in outer_split:

            # inner cross validation where we split rest data into train and dev
            if stratified:
                y_rest = y[idxs_rest]
                inner_cv = StratifiedKFold(n_splits=n_inner_splits, shuffle=shuffle, random_state=random_state)
                inner_split = inner_cv.split(idxs_rest, y_rest)
            else:
                inner_cv = KFold(n_splits=n_inner_splits, shuffle=shuffle, random_state=random_state)
                inner_split = inner_cv.split(idxs_rest)
            for idxs_train_idxs, idxs_dev_idxs in inner_split:

                # split idxs_rest with indexes from inner cross validation
                idxs_train = idxs_rest[idxs_train_idxs]
                idxs_dev = idxs_rest[idxs_dev_idxs]

                ds_train = Subset(ds_all, idxs_train)
                ds_dev = Subset(ds_all, idxs_dev)
                ds_test = Subset(ds_all, idxs_test)
                silos.append(DataSiloForCrossVal(datasilo, ds_train, ds_dev, ds_test))
        return silos
