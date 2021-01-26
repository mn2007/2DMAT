#!/usr/bin/env python
# -*- coding: utf-8 -*-

from abc import ABCMeta, abstractmethod
from enum import IntEnum
import time
import os

import numpy as np
from numpy.random import default_rng

from .. import exception, mpi

# for type hints
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING, Dict, Tuple
from ..runner.runner import Runner
from ..info import Info


if TYPE_CHECKING:
    from mpi4py import MPI


class AlgorithmStatus(IntEnum):
    INIT = 1
    PREPARE = 2
    RUN = 3


class AlgorithmBase(metaclass=ABCMeta):
    mpicomm: Optional["MPI.Comm"]
    mpisize: int
    mpirank: int
    rng: np.random.Generator
    dimension: int
    label_list: List[str]
    runner: Optional[Runner]

    root_dir: Path
    output_dir: Path
    proc_dir: Path

    timer: Dict[str, Dict] = {"prepare": {}, "run": {}, "post": {}}

    status: AlgorithmStatus = AlgorithmStatus.INIT

    @abstractmethod
    def __init__(self, info: Info) -> None:
        self.mpicomm = mpi.comm()
        self.mpisize = mpi.size()
        self.mpirank = mpi.rank()
        self.runner = None

        info_base = info["base"]
        if "dimension" not in info_base:
            raise exception.InputError(
                "ERROR: base.dimension is not defined in the input"
            )
        try:
            self.dimension = int(str(info_base["dimension"]))
        except ValueError:
            raise exception.InputError(
                "ERROR: base.dimension should be positive integer"
            )
        if self.dimension < 1:
            raise exception.InputError(
                "ERROR: base.dimension should be positive integer"
            )

        info_alg = info["algorithm"]
        if "label_list" in info_alg:
            label = info_alg["label_list"]
            if len(label) != self.dimension:
                raise exception.InputError(
                    f"ERROR: len(label_list) != dimension ({len(label)} != {self.dimension})"
                )
            self.label_list = label
        else:
            self.label_list = [f"x{d+1}" for d in range(self.dimension)]

        self.__init_rng(info)

        self.root_dir = info_base["root_dir"]
        self.output_dir = info_base["output_dir"]
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def __init_rng(self, info: Info) -> None:
        info_alg = info["algorithm"]
        seed = info_alg.get("seed", None)
        seed_delta = info_alg.get("seed_delta", 314159)

        if seed is None:
            self.rng = default_rng()
        else:
            self.rng = default_rng(seed + self.mpirank * seed_delta)

    def _read_param(self, info: Info) -> Tuple[np.array, np.array, np.array, np.array]:
        """

        Returns
        =======
        initial_list
        min_list
        max_list
        unit_list
        """
        info_algorithm = info["algorithm"]
        if "param" not in info_algorithm:
            raise exception.InputError(
                "ERROR: [algorithm.param] is not defined in the input"
            )
        info_param = info_algorithm["param"]

        if "min_list" not in info_param:
            raise exception.InputError(
                "ERROR: algorithm.param.min_list is not defined in the input"
            )
        min_list = np.array(info_param["min_list"])
        if len(min_list) != self.dimension:
            raise exception.InputError(
                f"ERROR: len(min_list) != dimension ({len(min_list)} != {self.dimension})"
            )

        if "max_list" not in info_param:
            raise exception.InputError(
                "ERROR: algorithm.param.max_list is not defined in the input"
            )
        max_list = np.array(info_param["max_list"])
        if len(max_list) != self.dimension:
            raise exception.InputError(
                f"ERROR: len(max_list) != dimension ({len(max_list)} != {self.dimension})"
            )

        unit_list = np.array(info_param.get("unit_list", [1.0] * self.dimension))
        if len(unit_list) != self.dimension:
            raise exception.InputError(
                f"ERROR: len(unit_list) != dimension ({len(unit_list)} != {self.dimension})"
            )

        initial_list = np.array(info_param.get("initial_list", []))
        if initial_list.size == 0:
            initial_list = min_list + (max_list - min_list) * self.rng.random(
                size=self.dimension
            )
        if initial_list.size != self.dimension:
            raise exception.InputError(
                f"ERROR: len(initial_list) != dimension ({initial_list.size} != {self.dimension})"
            )
        return initial_list, min_list, max_list, unit_list

    def set_runner(self, runner: Runner) -> None:
        self.runner = runner

    def prepare(self) -> None:
        if self.runner is None:
            msg = "Runner is not assigned"
            raise RuntimeError(msg)
        self._prepare()
        self.status = AlgorithmStatus.PREPARE

    @abstractmethod
    def _prepare(self) -> None:
        pass

    def run(self) -> None:
        if self.status < AlgorithmStatus.PREPARE:
            msg = "algorithm has not prepared yet"
            raise RuntimeError(msg)
        original_dir = os.getcwd()
        os.chdir(self.proc_dir)
        self._run()
        os.chdir(original_dir)
        self.status = AlgorithmStatus.RUN

    @abstractmethod
    def _run(self) -> None:
        pass

    def post(self) -> None:
        if self.status < AlgorithmStatus.RUN:
            msg = "algorithm has not run yet"
            raise RuntimeError(msg)
        original_dir = os.getcwd()
        os.chdir(self.output_dir)
        self._post()
        os.chdir(original_dir)

    @abstractmethod
    def _post(self) -> None:
        pass

    def main(self):
        time_sta = time.perf_counter()
        self.prepare()
        time_end = time.perf_counter()
        self.timer["prepare"]["total"] = time_end - time_sta
        if mpi.size() > 1:
            mpi.comm().Barrier()

        time_sta = time.perf_counter()
        self.run()
        time_end = time.perf_counter()
        self.timer["run"]["total"] = time_end - time_sta
        print("end of run")
        if mpi.size() > 1:
            mpi.comm().Barrier()

        time_sta = time.perf_counter()
        self.post()
        time_end = time.perf_counter()
        self.timer["post"]["total"] = time_end - time_sta

        with open(self.output_dir / f"time_rank{mpi.rank()}.log", "w") as fw:

            def output_file(type):
                tmp_dict = self.timer[type]
                fw.write("#{}\n total = {} [s]\n".format(type, tmp_dict["total"]))
                for key, t in tmp_dict.items():
                    if key == "total":
                        continue
                    fw.write(" - {} = {}\n".format(key, t))

            output_file("prepare")
            output_file("run")
            output_file("post")
