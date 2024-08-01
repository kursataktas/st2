# Copyright 2024 The StackStorm Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePath
from typing import DefaultDict

import yaml

from pants.backend.python.dependency_inference.module_mapper import (
    FirstPartyPythonMappingImpl,
    FirstPartyPythonMappingImplMarker,
    ModuleProvider,
    ModuleProviderType,
    ResolveName,
    module_from_stripped_path,
)
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import PythonResolveField, PythonSourceField
from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.base.specs import FileLiteralSpec, RawSpecs, RecursiveGlobSpec
from pants.engine.collection import Collection
from pants.engine.fs import DigestContents
from pants.engine.internals.native_engine import Address, Digest
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    AllTargets,
    AllUnexpandedTargets,
    HydrateSourcesRequest,
    HydratedSources,
    Target,
    Targets,
)
from pants.engine.unions import UnionRule
from pants.util.dirutil import fast_relpath
from pants.util.logging import LogLevel

from pack_metadata.target_types import (
    PackContentResourceSourceField,
    PackContentResourceTypeField,
    PackContentResourceTypes,
    PackMetadataSourcesField,
)


@dataclass(frozen=True)
class PackContentResourceTargetsOfTypeRequest:
    types: tuple[PackContentResourceTypes, ...]


class PackContentResourceTargetsOfType(Targets):
    pass


@rule(desc="Find all PackMetadata targets in project filtered by content type", level=LogLevel.DEBUG)
async def find_pack_metadata_targets_of_types(
    request: PackContentResourceTargetsOfTypeRequest, targets: AllTargets
) -> PackContentResourceTargetsOfType:
    return PackContentResourceTargetsOfType(
        tgt for tgt in targets
        if tgt.has_field(PackContentResourceSourceField)
        and (not request.types or tgt[PackContentResourceTypeField].value in request.types)
    )


@dataclass(frozen=True)
class PackContentPythonEntryPoint:
    metadata_address: Address
    content_type: PackContentResourceTypes
    entry_point: str
    python_address: Address
    resolve: str
    module: str


class PackContentPythonEntryPoints(Collection[PackContentPythonEntryPoint]):
    pass


class PackContentPythonEntryPointsRequest:
    pass


def get_possible_modules(path: PurePath) -> list[str]:
    if path.name in ("__init__.py", "__init__.pyi"):
        path = path.parent
    module = path.stem if path.suffix == ".py" else path.name
    modules = [module]

    try:
        start = path.parent.parts.index("actions") + 1
    except ValueError:
        start = path.parent.parts.index("sensors") + 1

    # st2 adds the parent dir of the python file to sys.path at runtime.
    # by convention, however, just actions/ is on sys.path during tests.
    # so, also construct the module name from actions/ to support tests.
    if start < len(path.parent.parts):
        modules.append(".".join((*path.parent.parts[start:], module)))
    return modules


@rule(desc="Find all Pack Content entry_points that are python", level=LogLevel.DEBUG)
async def find_pack_content_python_entry_points(
    python_setup: PythonSetup,
    _: PackContentPythonEntryPointsRequest
) -> PackContentPythonEntryPoints:
    action_or_sensor = (
        PackContentResourceTypes.action_metadata,
        PackContentResourceTypes.sensor_metadata,
    )

    action_and_sensor_metadata_targets = await Get(
        PackContentResourceTargetsOfType,
        PackContentResourceTargetsOfTypeRequest(action_or_sensor),
    )
    action_and_sensor_metadata_sources = await MultiGet(
        Get(HydratedSources, HydrateSourcesRequest(tgt[PackContentResourceSourceField]))
        for tgt in action_and_sensor_metadata_targets
    )
    action_and_sensor_metadata_contents = await MultiGet(
        Get(DigestContents, Digest, source.snapshot.digest)
        for source in action_and_sensor_metadata_sources
    )

    # python file path -> list of info about metadata files that refer to it
    pack_content_entry_points_by_spec: DefaultDict[
        str, list[tuple[Address, PackContentResourceTypes, str]]
    ] = defaultdict(list)

    tgt: Target
    contents: DigestContents
    for tgt, contents in zip(action_and_sensor_metadata_targets, action_and_sensor_metadata_contents):
        content_type = tgt[PackContentResourceTypeField].value
        if content_type not in action_or_sensor:
            continue
        assert len(contents) == 1
        try:
            metadata = yaml.safe_load(contents[0].content) or {}
        except yaml.YAMLError:
            continue
        if content_type == PackContentResourceTypes.action_metadata:
            runner_type = metadata.get("runner_type", "") or ""
            if runner_type != "python-script":
                # only python-script has special PYTHONPATH rules
                continue
        # get the entry_point to find subdirectory that contains the module
        entry_point = metadata.get("entry_point", "") or ""
        if entry_point:
            # address.filename is basically f"{spec_path}/{relative_file_path}"
            path = PurePath(tgt.address.filename).parent / entry_point
            pack_content_entry_points_by_spec[str(path)].append((tgt.address, content_type, entry_point))

    python_targets = await Get(
        Targets,
        RawSpecs(
            file_literals=tuple(FileLiteralSpec(spec_path) for spec_path in pack_content_entry_points_by_spec),
            unmatched_glob_behavior=GlobMatchErrorBehavior.ignore,
            description_of_origin="pack_metadata python module mapper",
        )
    )

    pack_content_entry_points: list[PackContentPythonEntryPoint] = []
    for tgt in python_targets:
        if not tgt.has_field(PythonResolveField):
            # this is unexpected
            continue
        for metadata_address, content_type, entry_point in pack_content_entry_points_by_spec[tgt.address.filename]:
            resolve = tgt[PythonResolveField].normalized_value(python_setup)

            for module in get_possible_modules(PurePath(tgt.address.filename)):
                pack_content_entry_points.append(PackContentPythonEntryPoint(
                    metadata_address=metadata_address,
                    content_type=content_type,
                    entry_point=entry_point,
                    python_address=tgt.address,
                    resolve=resolve,
                    module=module,
                ))

    return PackContentPythonEntryPoints(pack_content_entry_points)


@dataclass(frozen=True)
class PackPythonLib:
    pack_path: PurePath
    lib_dir: str
    relative_to_lib: PurePath
    python_address: Address
    resolve: str
    module: str


class PackPythonLibs(Collection[PackPythonLib]):
    pass


class PackPythonLibsRequest:
    pass


@rule(desc="Find all Pack lib directory python targets", level=LogLevel.DEBUG)
async def find_python_in_pack_lib_directories(
    python_setup: PythonSetup,
    all_unexpanded_targets: AllUnexpandedTargets,
    _: PackPythonLibsRequest,
) -> PackPythonLibs:
    pack_metadata_paths = [
        PurePath(tgt.address.spec_path) for tgt in all_unexpanded_targets
        if tgt.has_field(PackMetadataSourcesField)
    ]
    pack_lib_directory_targets = await MultiGet(
        Get(
            Targets,
            RawSpecs(
                recursive_globs=(
                    RecursiveGlobSpec(str(path / "lib")),
                    RecursiveGlobSpec(str(path / "actions" / "lib")),
                ),
                unmatched_glob_behavior=GlobMatchErrorBehavior.ignore,
                description_of_origin="pack_metadata lib directory lookup",
            )
        )
        for path in pack_metadata_paths
    )

    # Maybe this should use this to take codegen into account.
    # Get(PythonSourceFiles, PythonSourceFilesRequest(targets=lib_directory_targets, include_resources=False)
    # For now, just take the targets as they are.

    pack_python_libs: list[PackPythonLib] = []

    pack_path: PurePath
    lib_directory_targets: Targets
    for pack_path, lib_directory_targets in zip(pack_metadata_paths, pack_lib_directory_targets):
        for tgt in lib_directory_targets:
            if not tgt.has_field(PythonSourceField):
                # only python targets matter here.
                continue

            relative_to_pack = PurePath(fast_relpath(tgt[PythonSourceField].file_path, str(pack_path)))
            if relative_to_pack.parts[0] == "lib":
                lib_dir = "lib"
            elif relative_to_pack.parts[:2] == ("actions", "lib"):
                lib_dir = "actions/lib"
            else:
                # This should not happen as it is not in the requested glob.
                # Use this to tell linters that lib_dir is defined below here.
                continue
            relative_to_lib = relative_to_pack.relative_to(lib_dir)

            resolve = tgt[PythonResolveField].normalized_value(python_setup)
            module = module_from_stripped_path(relative_to_lib)

            pack_python_libs.append(PackPythonLib(
                pack_path=pack_path,
                lib_dir=lib_dir,
                relative_to_lib=relative_to_lib,
                python_address=tgt.address,
                resolve=resolve,
                module=module,
            ))

    return PackPythonLibs(pack_python_libs)


# This is only used to register our implementation with the plugin hook via unions.
class St2PythonPackContentMappingMarker(FirstPartyPythonMappingImplMarker):
    pass


@rule(desc="Creating map of pack_metadata targets to Python modules in pack content", level=LogLevel.DEBUG)
async def map_pack_content_to_python_modules(
    _: St2PythonPackContentMappingMarker,
) -> FirstPartyPythonMappingImpl:
    resolves_to_modules_to_providers: DefaultDict[
        ResolveName, DefaultDict[str, list[ModuleProvider]]
    ] = defaultdict(lambda: defaultdict(list))

    pack_content_python_entry_points, pack_python_libs = await MultiGet(
        Get(PackContentPythonEntryPoints, PackContentPythonEntryPointsRequest()),
        Get(PackPythonLibs, PackPythonLibsRequest()),
    )

    for pack_content in pack_content_python_entry_points:
        resolves_to_modules_to_providers[pack_content.resolve][pack_content.module].append(
            ModuleProvider(pack_content.python_address, ModuleProviderType.IMPL)
        )

    for pack_lib in pack_python_libs:
        provider_type = (
            ModuleProviderType.TYPE_STUB if pack_lib.relative_to_lib.suffix == ".pyi" else ModuleProviderType.IMPL
        )
        resolves_to_modules_to_providers[pack_lib.resolve][pack_lib.module].append(
            ModuleProvider(pack_lib.python_address, provider_type)
        )

    return FirstPartyPythonMappingImpl.create(resolves_to_modules_to_providers)


def rules():
    return (
        *collect_rules(),
        UnionRule(FirstPartyPythonMappingImplMarker, St2PythonPackContentMappingMarker),
    )
