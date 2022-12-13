import os
import shutil
import sys
from typing import Sequence

import yaml
from airflow.compat.functools import cached_property
from airflow.exceptions import AirflowException, AirflowSkipException
from airflow.hooks.base import BaseHook
from airflow.hooks.subprocess import SubprocessHook
from airflow.models.baseoperator import BaseOperator
from airflow.utils.context import Context
from airflow.utils.operator_helpers import context_to_airflow_vars


class DBTBaseOperator(BaseOperator):

    template_fields: Sequence[str] = ("env", "vars")
    ui_color = "#ed7254"

    def __init__(
        self,
        project_dir: str,
        conn_id: str,
        base_cmd: str = None,
        select: str = None,
        exclude: str = None,
        selector: str = None,
        vars: str = None,
        models: str = None,
        cache_selected_only: bool = False,
        no_version_check: bool = False,
        fail_fast: bool = False,
        quiet: bool = False,
        warn_error: bool = False,
        schema: str = None,
        env: dict = None,
        append_env: bool = False,
        output_encoding: str = "utf-8",
        skip_exit_code: int = 99,
        **kwargs,
    ) -> None:
        self.project_dir = project_dir
        self.conn_id = conn_id
        self.base_cmd = base_cmd
        self.select = select
        self.exclude = exclude
        self.selector = selector
        self.vars = vars
        self.models = models
        self.cache_selected_only = cache_selected_only
        self.no_version_check = no_version_check
        self.fail_fast = fail_fast
        self.quiet = quiet
        self.warn_error = warn_error
        self.schema = schema
        self.env = env
        self.append_env = append_env
        self.output_encoding = output_encoding
        self.skip_exit_code = skip_exit_code
        super().__init__(**kwargs)

    @cached_property
    def subprocess_hook(self):
        """Returns hook for running the bash command."""
        return SubprocessHook()

    def get_env(self, context):
        """Builds the set of environment variables to be exposed for the bash command."""
        system_env = os.environ.copy()
        env = self.env
        if env is None:
            env = system_env
        else:
            if self.append_env:
                system_env.update(env)
                env = system_env

        airflow_context_vars = context_to_airflow_vars(context, in_env_var_format=True)
        self.log.debug(
            "Exporting the following env vars:\n%s",
            "\n".join(f"{k}={v}" for k, v in airflow_context_vars.items()),
        )
        env.update(airflow_context_vars)

        return env

    def get_dbt_path(self):
        dbt_path = shutil.which("dbt") or "dbt"
        if self.project_dir is not None:
            if not os.path.exists(self.project_dir):
                raise AirflowException(
                    f"Can not find the project_dir: {self.project_dir}"
                )
            if not os.path.isdir(self.project_dir):
                raise AirflowException(
                    f"The project_dir {self.project_dir} must be a directory"
                )
        return dbt_path

    def exception_handling(self, result):
        if self.skip_exit_code is not None and result.exit_code == self.skip_exit_code:
            raise AirflowSkipException(
                f"dbt command returned exit code {self.skip_exit_code}. Skipping."
            )
        elif result.exit_code != 0:
            raise AirflowException(
                f"dbt command failed. The command returned a non-zero exit code {result.exit_code}."
            )

    def add_global_flags(self):

        global_flags = [
            "project_dir",
            "select",
            "exclude",
            "selector",
            "vars",
            "models",
        ]

        flags = []
        for global_flag in global_flags:
            dbt_name = f"--{global_flag.replace('_', '-')}"
            global_flag_value = self.__getattribute__(global_flag)
            if global_flag_value is not None:
                flags.append(dbt_name)
                flags.append(str(global_flag_value))

        global_boolean_flags = [
            "no_version_check",
            "cache_selected_only",
            "fail_fast",
            "quiet",
            "warn_error",
        ]
        for global_boolean_flag in global_boolean_flags:
            dbt_name = f"--{global_boolean_flag.replace('_', '-')}"
            global_boolean_flag_value = self.__getattribute__(global_boolean_flag)
            if global_boolean_flag_value is True:
                flags.append(dbt_name)
        return flags

    def build_command(self):
        dbt_path = self.get_dbt_path()
        cmd = [dbt_path, self.base_cmd] + self.add_global_flags()
        return cmd

    def run_command(self, cmd, env):
        result = self.subprocess_hook.run_command(
            command=cmd,
            env=env,
            output_encoding=self.output_encoding,
            cwd=self.project_dir,
        )
        self.exception_handling(result)
        return result

    def create_default_profiles(self):
        profile = {
            "postgres_profile": {
                "outputs": {
                    "dev": {
                        "type": "postgres",
                        "host": "{{ env_var('POSTGRES_HOST') }}",
                        "port": "{{ env_var('POSTGRES_PORT') | as_number }}",
                        "user": "{{ env_var('POSTGRES_USER') }}",
                        "pass": "{{ env_var('POSTGRES_PASSWORD') }}",
                        "dbname": "{{ env_var('POSTGRES_DATABASE') }}",
                        "schema": "{{ env_var('POSTGRES_SCHEMA') }}",
                    }
                },
                "target": "dev",
            }
        }
        # Define the path to the directory and file
        directory_path = "/home/astro/.dbt"
        file_path = "/home/astro/.dbt/profiles.yml"

        # Create the directory if it does not exist
        if not os.path.exists(directory_path):
            os.makedirs(directory_path)

        # Create the file if it does not exist
        if not os.path.exists(file_path):
            print("profiles.yml not found - initializing.")
            with open(file_path, "w") as file:
                yaml.dump(profile, file)
            print("done")
        else:
            print("profiles.yml found - skipping")

    def map_profile(self):
        conn = BaseHook().get_connection(self.conn_id)

        if conn.conn_type == "postgres":
            profile = "postgres_profile"
            profile_vars = {
                "POSTGRES_HOST": conn.host,
                "POSTGRES_USER": conn.login,
                "POSTGRES_PASSWORD": conn.password,
                "POSTGRES_DATABASE": conn.schema,
                "POSTGRES_PORT": str(conn.port),
                "POSTGRES_SCHEMA": self.schema,
            }

        else:
            print(f"Connection type {conn.type} is not yet supported.", file=sys.stderr)
            sys.exit(1)

        return profile, profile_vars

    def execute(self, context: Context):
        self.create_default_profiles()
        profile, profile_vars = self.map_profile()
        env = self.get_env(context) | profile_vars
        target_flag = ["--profile", profile]
        cmd = self.build_command() + target_flag
        result = self.run_command(cmd=cmd, env=env)
        return result.output