# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Example script for exporting Llama2 to flatbuffer

import argparse
import copy
import json
import logging
import shlex
from enum import Enum
from json import JSONDecodeError
from pathlib import Path
from typing import Optional, Union

import pkg_resources

import torch

from executorch.examples.models.llama2.llama_transformer import ModelArgs

from executorch.extension.llm.export.builder import DType, LLMEdgeManager

from executorch.extension.llm.export.partitioner_lib import (
    get_coreml_partitioner,
    get_mps_partitioner,
    get_qnn_partitioner,
    get_vulkan_partitioner,
    get_xnnpack_partitioner,
)

from executorch.extension.llm.export.quantizer_lib import (
    get_pt2e_quantization_params,
    get_pt2e_quantizers,
    get_qnn_quantizer,
)

from executorch.sdk.etrecord import generate_etrecord
from executorch.util.activation_memory_profiler import generate_memory_trace

from ..model_factory import EagerModelFactory
from .source_transformation.quantize import (
    get_quant_embedding_transform,
    get_quant_weight_transform,
)
from .source_transformation.rope import materialze_broadcast_of_rope_freq_cis
from .source_transformation.sdpa import (
    replace_causal_mask,
    replace_sdpa_with_custom_op,
    replace_sdpa_with_simple_sdpa,
)

IS_FBCODE = True  #  os.environ.get("FBCODE_PLATFORM", False)
FORMAT = "[%(levelname)s %(asctime)s %(filename)s:%(lineno)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

pkg_name = __name__
verbosity_setting = None


class WeightType(Enum):
    LLAMA = "LLAMA"
    FAIRSEQ2 = "FAIRSEQ2"


def set_pkg_name(name: str) -> None:
    global pkg_name
    pkg_name = name


def get_resource_path(resource_name) -> str:
    return pkg_resources.resource_filename(pkg_name, resource_name)


def set_verbosity(val):
    global verbosity_setting
    verbosity_setting = val


def verbose_export():
    return verbosity_setting


def build_model(
    modelname: str = "model",
    extra_opts: str = "",
    *,
    par_local_output: bool = False,
    resource_pkg_name: str = __name__,
) -> str:
    if False:  # par_local_output:
        output_dir_path = "par:."
    else:
        output_dir_path = "."

    argString = f"--checkpoint par:{modelname}_ckpt.pt --params par:{modelname}_params.json {extra_opts} --output-dir {output_dir_path}"
    parser = build_args_parser()
    args = parser.parse_args(shlex.split(argString))
    # pkg_name = resource_pkg_name
    return export_llama(modelname, args)


def build_args_parser() -> argparse.ArgumentParser:
    ckpt_dir = f"{Path(__file__).absolute().parent.as_posix()}"
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output-dir", default=".", help="output directory")
    # parser.add_argument(
    #     "-q", "--quantized_ckpt", default=None, help="quantized checkpoint file"
    # )
    parser.add_argument(
        "-E",
        "--embedding-quantize",
        default=None,
        type=str,
        help="type of embedding quantization, '<bitwidth>,<groupsize>', e.g., '8,1024'.",
    )
    parser.add_argument(
        "--pt2e_quantize",
        default=None,
        choices=[
            "xnnpack_dynamic",
            "xnnpack_dynamic_qc4",
            "qnn_8a8w",
            "qnn_16a16w",
            "qnn_16a4w",
        ],
        help="Use PT2E quantization. Comma separated options. e.g. xnnpack_dynamic (for per channel 8 bit weight), xnnpack_dynamic_qc4 (for per channel 4 bit weight), embedding.",
    )
    parser.add_argument(
        "-qmode",
        "--quantization_mode",
        type=str,
        default=None,
        choices=["int8", "8da4w", "8da4w-gptq"],
        help="type of quantization",
    )

    parser.add_argument(
        "-c",
        "--checkpoint",
        default=f"{ckpt_dir}/params/demo_rand_params.pth",
        help="checkpoint path",
    )

    parser.add_argument(
        "--checkpoint_dir",
        default=None,
        help="checkpoint directory. Use with a sharded checkpoint, not for the standard llama2 model. Note, checkpoint_dir takes precedence over checkpoint if both are set.",
    )

    parser.add_argument(
        "--calibration_tasks",
        nargs="+",
        type=str,
        default=None,
        help="Tasks for GPTQ calibration",
    )
    parser.add_argument(
        "--calibration_limit",
        type=int,
        default=None,
        help="number of samples used for calibration",
    )
    parser.add_argument(
        "--calibration_seq_length",
        type=int,
        default=None,
        help="Sequence length for GPTQ calibration",
    )
    parser.add_argument(
        "-t",
        "--tokenizer_path",
        default=None,
        help="tokenizer path (Note: .model not .bin)",
    )
    parser.add_argument(
        "-kv",
        "--use_kv_cache",
        default=False,
        action="store_true",
        help="Whether or not to export a model using kv cache",
    )
    parser.add_argument(
        "--use_sdpa_with_kv_cache",
        default=False,
        action="store_true",
        help="Whether to use sdpa_with_kv_cache update op when using kv cache",
    )
    parser.add_argument(
        "--disable_dynamic_shape",
        dest="enable_dynamic_shape",
        default=True,  # Enable this by default
        action="store_false",
        help="Enable dynamic shape along seq dim. Used for faster prefill",
    )
    parser.add_argument(
        "-p",
        "--params",
        default=f"{ckpt_dir}/params/demo_config.json",
        help="config.json",
    )
    parser.add_argument(
        "-m",
        "--metadata",
        default=None,
        help='metadata string in json format. Example {"key": 1, "key2": "value2"}',
    )
    parser.add_argument(
        "-s",
        "--so_library",
        default=None,
        required=False,
        help="shared library for quantized operators",
    )
    parser.add_argument(
        "--profile_memory",
        required=False,
        action="store_true",
        help="Generate chrome trace of activation memory for intermediate tensors.",
    )
    parser.add_argument(
        "-prof",
        "--profile_path",
        default=None,
        help="Use cProfile to profile model export. Results saved to profile_path as a html file.",
    )
    parser.add_argument(
        "-G",
        "--group_size",
        type=int,
        default=None,
        help="group_size for weight quantization",
    )

    parser.add_argument(
        "-d",
        "--dtype-override",
        default="fp32",
        type=str,
        choices=["fp32", "fp16"],
        help="Override the dtype of the model (default is the checkpoint dtype)."
        "Options: fp32, fp16. Please be aware that only some backends support fp16.",
    )

    parser.add_argument(
        "-n",
        "--output_name",
        default=None,
        help="Override the output filename of the saved pte model file.",
    )

    parser.add_argument(
        "--max_seq_length",
        type=int,
        default=128,
        help="maximum length sequence to evaluate",
    )

    parser.add_argument("-2", "--fairseq2", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-X", "--xnnpack", action="store_true")
    parser.add_argument("-V", "--vulkan", action="store_true")
    parser.add_argument("--mps", action="store_true")
    parser.add_argument("--coreml", action="store_true")
    parser.add_argument(
        "--qnn",
        action="store_true",
        help="Delegate llama2 to qnn backend (Qualcomm), please use it --kv_cahce=True",
    )

    parser.add_argument(
        "--expand_rope_table",
        default=False,
        action="store_true",
        help="[Temp workaround] Expand sin/cos table in head dim to take vectorized path in optimized kernels.",
    )

    parser.add_argument(
        "--generate_etrecord",
        action="store_true",
        required=False,
        default=False,
        help="Generate the ETRecord debug artifact.",
    )

    return parser


def canonical_path(path: Union[str, Path], *, dir: bool = False) -> str:

    path = str(path)

    if verbose_export():
        print(f"creating canonical path for {path}")

    if not path.startswith("par:"):
        return path

    if not IS_FBCODE:
        print("not FBCODE")
        return path[4:]
    else:
        return_val = pkg_resources.resource_filename(pkg_name, path[4:])
        if verbose_export():
            print(f"canonical name is: {return_val}")
        return return_val


def export_llama(modelname, args) -> str:
    if args.profile_path is not None:
        try:
            from executorch.util.python_profiler import CProfilerFlameGraph

            with CProfilerFlameGraph(args.profile_path):
                builder = _export_llama(modelname, args)
                assert (
                    filename := builder.get_saved_pte_filename()
                ) is not None, "Fail to get file name from builder"
                return filename
        except ImportError:
            print(
                "Please run `pip install snakeviz` to install required dependencies for cProfiler flamegraph."
            )
            return ""
    else:
        builder = _export_llama(modelname, args)
        assert (
            filename := builder.get_saved_pte_filename()
        ) is not None, "Fail to get file name from builder"
        return filename


def _prepare_for_llama_export(modelname: str, args) -> LLMEdgeManager:
    """
    Helper function for export_llama. Loads the model from checkpoint and params,
    and sets up a LLMEdgeManager with initial transforms and dtype conversion.

    Returns a LLMEdgeManager prior to calling export_to_edge with quantizers
    """

    # load model from checkpoint and params.json
    checkpoint_path = canonical_path(args.checkpoint) if args.checkpoint else None
    checkpoint_dir = (
        canonical_path(args.checkpoint_dir) if args.checkpoint_dir else None
    )
    params_path = canonical_path(args.params)
    output_dir_path = canonical_path(args.output_dir, dir=True)
    weight_type = WeightType.FAIRSEQ2 if args.fairseq2 else WeightType.LLAMA

    # dtype override
    if args.dtype_override is not None:
        dtype_override = DType[args.dtype_override]
    elif args.quantization_mode in ["8da4w", "8da4w-gptq"]:
        dtype_override = DType["fp16"]
    else:
        dtype_override = None

    # source transforms
    transforms = []
    if args.quantization_mode:
        modelname = f"{modelname}_q"
        transforms.append(
            get_quant_weight_transform(args, dtype_override, verbose_export())
        )

    if args.embedding_quantize:
        modelname = f"{modelname}_e"
        transforms.append(get_quant_embedding_transform(args))

    if args.expand_rope_table:
        transforms.append(materialze_broadcast_of_rope_freq_cis)

    if args.use_sdpa_with_kv_cache:
        transforms.append(replace_sdpa_with_custom_op)

    if args.use_kv_cache:
        if args.qnn or args.coreml or args.mps:
            # Currently qnn/coreml/mps doesn't support sdpa op, use the simpler decomposition
            # to get free perf gain.
            transforms.append(replace_sdpa_with_simple_sdpa)
            transforms.append(replace_causal_mask)
    return (
        _load_llama_model(
            modelname=modelname,
            checkpoint=checkpoint_path,
            checkpoint_dir=checkpoint_dir,
            params_path=params_path,
            use_kv_cache=args.use_kv_cache,
            use_sdpa_with_kv_cache=args.use_sdpa_with_kv_cache,
            weight_type=weight_type,
            enable_dynamic_shape=args.enable_dynamic_shape,
            verbose=args.verbose,
            max_seq_len=args.max_seq_length,
            metadata_str=args.metadata,
        )
        .set_output_dir(output_dir_path)
        .to_dtype(dtype_override)
        .source_transform(transforms)
    )


def get_quantizer_and_quant_params(args):
    pt2e_quant_params = get_pt2e_quantization_params(
        args.pt2e_quantize, args.quantization_mode
    )
    quantizers = get_pt2e_quantizers(pt2e_quant_params, args.so_library)
    quant_dtype = None
    if args.qnn and args.pt2e_quantize:
        assert len(quantizers) == 0, "Should not enable both xnnpack and qnn"
        qnn_quantizer, quant_dtype = get_qnn_quantizer(
            args.pt2e_quantize, args.quantization_mode
        )
        quantizers.append(qnn_quantizer)
    logging.info(f"Applying quantizers: {quantizers}")
    return pt2e_quant_params, quantizers, quant_dtype


def _validate_args(args):
    """
    TODO: Combine all the backends under --backend args
    """
    if args.enable_dynamic_shape and (args.coreml or args.mps or args.qnn):
        raise ValueError(
            "Dynamic shape is not supported with coreml, MPS or qnn backends."
            " Please us --disble_dynamic_shape."
        )


def _export_llama(modelname, args) -> LLMEdgeManager:  # noqa: C901
    _validate_args(args)
    pt2e_quant_params, quantizers, quant_dtype = get_quantizer_and_quant_params(args)

    # export_to_edge
    builder_exported_to_edge = (
        _prepare_for_llama_export(modelname, args)
        .capture_pre_autograd_graph()
        .pt2e_quantize(quantizers)
        .export_to_edge()
    )

    modelname = builder_exported_to_edge.modelname

    # to_backend
    partitioners = []
    if pt2e_quant_params is not None and pt2e_quant_params.quantize_linear is not None:
        partitioners.append(get_xnnpack_partitioner())
        modelname = f"xnnpack_dq_{modelname}"

    if args.xnnpack:
        partitioners.append(get_xnnpack_partitioner())
        modelname = f"xnnpack_{modelname}"

    if args.vulkan:
        partitioners.append(
            get_vulkan_partitioner(
                args.dtype_override,
                args.quantization_mode,
            )
        )
        modelname = f"vulkan_{modelname}"

    if args.mps:
        partitioners.append(get_mps_partitioner(args.use_kv_cache))
        modelname = f"mps_{modelname}"

    if args.coreml:
        partitioners.append(get_coreml_partitioner(args.use_kv_cache))
        modelname = f"coreml_{modelname}"

    if args.qnn:
        partitioners.append(
            get_qnn_partitioner(
                quant_dtype,
                args.use_kv_cache,
                args.pt2e_quantize,
            )
        )
        # pyre-ignore: Undefined import [21]: Could not find a module corresponding to import `executorch.backends.qualcomm.utils.utils`
        from executorch.backends.qualcomm.utils.utils import _transform

        # pyre-ignore: Undefined attribute [16]: Module `executorch.backends` has no attribute `qualcomm`, Optional type has no attribute `exported_program`
        _transform(builder_exported_to_edge.edge_manager.exported_program())

    if args.generate_etrecord:
        if not builder_exported_to_edge.edge_manager:
            raise ValueError("Unable to generate etrecord due to missing edge manager.")

        logging.info("Generating etrecord")
        # Copy the edge manager which will be serialized into etrecord. This is memory-wise expensive.
        edge_manager_copy = copy.deepcopy(builder_exported_to_edge.edge_manager)
        builder = builder_exported_to_edge.to_backend(partitioners).to_executorch()

        # Generate ETRecord
        if edge_manager_copy:
            generate_etrecord(
                et_record="etrecord.bin",
                edge_dialect_program=edge_manager_copy,
                executorch_program=builder.export_program,
            )
            logging.info("Generated etrecord.bin")
    else:
        builder = builder_exported_to_edge.to_backend(partitioners).to_executorch()

    if args.profile_memory:
        generate_memory_trace(builder.export_program, "memory_profile.json")

    if builder.dtype == DType.fp16:
        modelname = f"{modelname}_h"

    if args.output_name:
        modelname = args.output_name
        if modelname.endswith(".pte"):
            output_file = modelname
            modelname = modelname[:-4]
            print(f"modelname: {modelname}")
            print(f"output_file: {output_file}")
        else:
            output_file = f"{builder.output_dir}/{modelname}.pte"
            print(f"modelname: {modelname}")
            print(f"output_file: {output_file}")
    else:
        output_file = f"{builder.output_dir}/{modelname}.pte"

    builder.save_to_pte(output_file)

    return builder


def _load_llama_model_metadata(
    weight_type: WeightType,
    dtype: DType,
    use_kv_cache: bool,
    use_sdpa_with_kv_cache: bool,
    enable_dynamic_shape: bool,
    modelArgs: ModelArgs,
    metadata_str: Optional[str] = None,
):
    is_fairseq2 = weight_type == WeightType.FAIRSEQ2
    metadata = {
        "append_eos_to_prompt": is_fairseq2,  # For language llama, tell the runtime to always append EOS token(s) to prompt.
        "get_bos_id": 3 if is_fairseq2 else 1,
        "get_dtype": 5 if dtype == DType.fp16 else 6,
        "get_eos_id": 3 if is_fairseq2 else 2,
        "get_head_dim": modelArgs.dim // modelArgs.n_heads,
        "get_max_batch_size": modelArgs.max_batch_size,
        "get_max_seq_len": modelArgs.max_seq_len,
        "get_n_bos": 1,
        "get_n_eos": 2 if is_fairseq2 else 1,
        "get_n_kv_heads": modelArgs.n_kv_heads,
        "get_n_layers": modelArgs.n_layers,
        "get_vocab_size": modelArgs.vocab_size,
        "use_kv_cache": use_kv_cache,
        "use_sdpa_with_kv_cache": use_sdpa_with_kv_cache,
        "enable_dynamic_shape": enable_dynamic_shape,
    }
    if metadata_str:
        try:
            extra = json.loads(metadata_str)
            for k, v in extra.items():
                metadata[k] = v
        except JSONDecodeError:
            logging.error("Invalid metadata, should be a valid JSON string")
    return metadata


def _load_llama_model(
    *,
    modelname: str = "llama2",
    checkpoint: Optional[str] = None,
    checkpoint_dir: Optional[str] = None,
    params_path: str,
    use_kv_cache: bool = False,
    use_sdpa_with_kv_cache: bool = False,
    weight_type: WeightType = WeightType.LLAMA,
    enable_dynamic_shape: bool = False,
    verbose: bool = False,
    max_seq_len: int = 128,
    metadata_str: Optional[str] = None,
) -> "LLMEdgeManager":
    """
    A helper util that builds a Llama2 model. It returns a LLMEdgeManager that
    can help further lower the model to ExecuTorch.
    Returns:
        An instance of LLMEdgeManager which contains the eager mode model.
    """
    assert (
        checkpoint or checkpoint_dir
    ) and params_path, "Both checkpoint/checkpoint_dir and params can't be empty"
    logging.info(
        f"Loading model with checkpoint={checkpoint}, params={params_path}, use_kv_cache={use_kv_cache}, weight_type={weight_type}"
    )
    model, example_inputs, _ = EagerModelFactory.create_model(
        "llama2",
        "Llama2Model",
        checkpoint=checkpoint,
        checkpoint_dir=checkpoint_dir,
        params=params_path,
        use_kv_cache=use_kv_cache,
        use_sdpa_with_kv_cache=use_sdpa_with_kv_cache,
        fairseq2=weight_type == WeightType.FAIRSEQ2,
        max_seq_len=max_seq_len,
        enable_dynamic_shape=enable_dynamic_shape,
    )
    state_dict = model.state_dict()
    dtype = state_dict[next(iter(state_dict))].dtype
    assert dtype in [
        torch.bfloat16,
        torch.float16,
        torch.float32,
    ], f"Only support bfloat16, fp16 or fp32 got {dtype}"
    logging.info(f"Loaded model with dtype={dtype}")

    if dtype == torch.bfloat16:
        dtype = DType.bf16
    elif dtype == torch.float16:
        dtype = DType.fp16
    elif dtype == torch.float32:
        dtype = DType.fp32
    else:
        raise ValueError(f"Unsupported dtype {dtype}")

    return LLMEdgeManager(
        model=model,
        modelname=modelname,
        max_seq_len=model.params.max_seq_len,
        dtype=dtype,
        use_kv_cache=use_kv_cache,
        example_inputs=example_inputs,
        enable_dynamic_shape=enable_dynamic_shape,
        verbose=verbose,
        metadata=_load_llama_model_metadata(
            weight_type,
            dtype,
            use_kv_cache,
            use_sdpa_with_kv_cache,
            enable_dynamic_shape,
            model.params,
            metadata_str,
        ),
    )
