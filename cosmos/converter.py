from __future__ import annotations

import inspect
import logging
import pathlib
from enum import Enum
from typing import Any, Callable, Optional

from airflow.exceptions import AirflowException
from airflow.models.dag import DAG
from airflow.utils.task_group import TaskGroup

from cosmos.airflow.graph import build_airflow_graph
from cosmos.constants import ExecutionMode, LoadMode, TestBehavior
from cosmos.dbt.executable import get_system_dbt
from cosmos.dbt.graph import DbtGraph
from cosmos.dbt.project import DbtProject
from cosmos.dbt.selector import retrieve_by_label
from cosmos.profiles import ProfileConfig


logger = logging.getLogger(__name__)


class UserInputError(Exception):
    pass


def specific_kwargs(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    Extract kwargs specific to the cosmos.converter.DbtToAirflowConverter class initialization method.

    :param kwargs: kwargs which can contain DbtToAirflowConverter and non DbtToAirflowConverter kwargs.
    """
    new_kwargs = {}
    specific_args_keys = inspect.getfullargspec(DbtToAirflowConverter.__init__).args
    for arg_key, arg_value in kwargs.items():
        if arg_key in specific_args_keys:
            new_kwargs[arg_key] = arg_value
    return new_kwargs


def airflow_kwargs(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    Extract kwargs specific to the Airflow DAG or TaskGroup class initialization method.

    :param kwargs: kwargs which can contain Airflow DAG or TaskGroup and cosmos.converter.DbtToAirflowConverter kwargs.
    """
    new_kwargs = {}
    non_airflow_kwargs = specific_kwargs(**kwargs)
    for arg_key, arg_value in kwargs.items():
        if arg_key not in non_airflow_kwargs:
            new_kwargs[arg_key] = arg_value
    return new_kwargs


def validate_arguments(select: list[str], exclude: list[str], task_args: dict[str, Any]) -> None:
    """
    Validate that mutually exclusive selectors filters have not been given.
    Validate deprecated arguments.

    :param select: A list of dbt select arguments (e.g. 'config.materialized:incremental')
    :param exclude: A list of dbt exclude arguments (e.g. 'tag:nightly')
    :param profile_args: Arguments to pass to the dbt profile
    :param task_args: Arguments to be used to instantiate an Airflow Task
    """
    for field in ("tags", "paths"):
        select_items = retrieve_by_label(select, field)
        exclude_items = retrieve_by_label(exclude, field)
        intersection = {str(item) for item in set(select_items).intersection(exclude_items)}
        if intersection:
            raise AirflowException(f"Can't specify the same {field[:-1]} in `select` and `exclude`: " f"{intersection}")

    # if task_args has a schema, add it to the profile args and add a deprecated warning
    if "schema" in task_args:
        raise AirflowException("The `schema` argument is no longer supported. Please use the `profile_args` instead.")


def convert_value_to_enum(value: str | Enum, enum_class: Enum) -> Enum:
    """
    If value is an enum, return enum item.
    Else, if value is a string, attempt to return the correspondent enum value.
    Raise an exception otherwise
    """
    if isinstance(value, str):
        try:
            return enum_class(value)
        except ValueError:
            raise UserInputError(f"The given value {value} is not compatible with the type {enum_class.__name__}")
    else:
        return value


class DbtToAirflowConverter:
    """
    Logic common to build an Airflow DbtDag and DbtTaskGroup from a DBT project.

    :param dag: Airflow DAG to be populated
    :param task_group (optional): Airflow Task Group to be populated
    :param profile_config: A ProfileConfig object to use to render and execute dbt. Required if using
        local or virtualenv execution mode.
    :param dbt_project_name: The name of the dbt project
    :param dbt_root_path: The path to the dbt root directory
    :param dbt_models_dir: The path to the dbt models directory within the project
    :param dbt_seeds_dir: The path to the dbt seeds directory within the project
    :param dbt_args: Parameters to pass to the underlying dbt operators, can include dbt_executable_path to utilize venv
    :param operator_args: Parameters to pass to the underlying operators, can include KubernetesPodOperator
        or DockerOperator parameters
    :param emit_datasets: If enabled test nodes emit Airflow Datasets for downstream cross-DAG dependencies
    :param test_behavior: When to run `dbt` tests. Default is TestBehavior.AFTER_EACH, that runs tests after each model.
    :param select: A list of dbt select arguments (e.g. 'config.materialized:incremental')
    :param exclude: A list of dbt exclude arguments (e.g. 'tag:nightly')
    :param execution_mode: Where Cosmos should run each dbt task (e.g. ExecutionMode.LOCAL, ExecutionMode.KUBERNETES).
        Default is ExecutionMode.LOCAL.
    :param on_warning_callback: A callback function called on warnings with additional Context variables "test_names"
        and "test_results" of type `List`. Each index in "test_names" corresponds to the same index in "test_results".
    """

    def __init__(
        self,
        dbt_project_name: str,
        profile_config: ProfileConfig | None = None,
        dag: DAG | None = None,
        task_group: TaskGroup | None = None,
        dbt_args: dict[str, Any] = {},
        operator_args: dict[str, Any] = {},
        emit_datasets: bool = True,
        dbt_root_path: str = "/usr/local/airflow/dags/dbt",
        dbt_models_dir: str | None = None,
        dbt_seeds_dir: str | None = None,
        dbt_snapshots_dir: str | None = None,
        test_behavior: str | TestBehavior = TestBehavior.AFTER_EACH,
        select: list[str] | None = None,
        exclude: list[str] | None = None,
        execution_mode: str | ExecutionMode = ExecutionMode.LOCAL,
        load_mode: str | LoadMode = LoadMode.AUTOMATIC,
        manifest_path: str | pathlib.Path | None = None,
        on_warning_callback: Optional[Callable] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        select = select or []
        exclude = exclude or []

        if execution_mode in ["local", "virtualenv"] and not profile_config:
            raise ValueError(
                "When using local or virtualenv execution mode, a ProfileConfig must be provided to render the profile."
            )

        test_behavior = convert_value_to_enum(test_behavior, TestBehavior)
        execution_mode = convert_value_to_enum(execution_mode, ExecutionMode)
        load_mode = convert_value_to_enum(load_mode, LoadMode)

        dbt_project = DbtProject(
            name=dbt_project_name,
            root_dir=dbt_root_path,
            models_dir=dbt_models_dir,
            seeds_dir=dbt_seeds_dir,
            snapshots_dir=dbt_snapshots_dir,
            manifest_path=manifest_path,
        )

        dbt_graph = DbtGraph(
            project=dbt_project,
            exclude=exclude,
            select=select,
            dbt_cmd=dbt_args.get("dbt_executable_path", get_system_dbt()),
        )
        dbt_graph.load(method=load_mode, execution_mode=execution_mode)

        task_args = {
            **dbt_args,
            **operator_args,
            # the following args may be only needed for local / venv:
            "project_dir": dbt_project.dir,
            "profile_config": profile_config,
        }

        validate_arguments(select, exclude, task_args)

        build_airflow_graph(
            nodes=dbt_graph.nodes,
            dag=dag or (task_group and task_group.dag),
            task_group=task_group,
            execution_mode=execution_mode,
            task_args=task_args,
            test_behavior=test_behavior,
            dbt_project_name=dbt_project.name,
            on_warning_callback=on_warning_callback,
            emit_datasets=emit_datasets,
        )
