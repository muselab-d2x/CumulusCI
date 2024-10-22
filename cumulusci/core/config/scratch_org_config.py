import contextlib
import datetime
import json
import os
from tempfile import NamedTemporaryFile
from typing import List, NoReturn, Optional

import sarge

from cumulusci.core.config import FAILED_TO_CREATE_SCRATCH_ORG
from cumulusci.core.config.sfdx_org_config import SfdxOrgConfig
from cumulusci.core.org_history import (
    ActionScratchDefReference,
    OrgCreateAction,
    OrgDeleteAction,
)
from cumulusci.core.exceptions import (
    CumulusCIException,
    ScratchOrgException,
    ServiceNotConfigured,
)
from cumulusci.core.sfdx import sfdx


class ScratchOrgConfig(SfdxOrgConfig):
    """Salesforce DX Scratch org configuration"""

    noancestors: bool
    # default = None  # what is this?
    instance: str
    password_failed: bool
    devhub: str
    release: str

    createable: bool = True

    @property
    def scratch_info(self):
        """Deprecated alias for sfdx_info.

        Will create the scratch org if necessary.
        """
        return self.sfdx_info

    @property
    def days(self) -> int:
        return self.config.setdefault("days", 1)

    @property
    def active(self) -> bool:
        """Check if an org is alive"""
        return self.date_created and not self.expired

    @property
    def expired(self) -> bool:
        """Check if an org has already expired"""
        return bool(self.expires) and self.expires < datetime.datetime.utcnow()

    @property
    def expires(self) -> Optional[datetime.datetime]:
        if self.date_created:
            return self.date_created + datetime.timedelta(days=int(self.days))

    @property
    def days_alive(self) -> Optional[int]:
        if self.date_created and not self.expired:
            delta = datetime.datetime.utcnow() - self.date_created
            return delta.days + 1

    def create_org(self) -> None:
        """Uses sfdx force:org:create to create the org"""
        if not self.config_file:
            raise ScratchOrgException(
                f"Scratch org config {self.name} is missing a config_file"
            )
        if not self.scratch_org_type:
            self.config["scratch_org_type"] = "workspace"

        config = ActionScratchDefReference(
            path=self.config_file,
        )

        org_action_info = {
            "timestamp": datetime.datetime.now().timestamp(),
            "action_type": "OrgCreate",
            "repo": self.keychain.project_config.repo_url,
            "branch": self.keychain.project_config.repo_branch,
            "commit": self.keychain.project_config.repo_commit,
            "scratch_org": self.keychain.project_config.lookup(
                f"orgs__scratch__{self.config_name}"
            ),
            "config": {
                "source_path": os.path.abspath(self.config_file),
                "path": os.path.abspath(
                    self.config_file
                ),  # This will be updated if we use a temp file
            },
            "days": self.days,
            "namespaced": self.namespaced,
            "log": "",
        }

        @contextlib.contextmanager
        def temp_scratchdef_file():
            snapshot_description = None
            if self.use_snapshot_hashes:
                if not self.snapshot_hashes:
                    raise ScratchOrgException(
                        "Snapshot hashes are required when use_snapshot_hashes is True"
                    )
                hashed_snapshot = self.snapshot_hashes[-1]
                snapshot_name = hashed_snapshot.get("snapshot", {}).get("SnapshotName")
                if not snapshot_name:
                    raise ScratchOrgException(
                        "Snapshot hashes must contain a snapshot with a SnapshotName"
                    )
                snapshot_hash = hashed_snapshot.get("snapshot_hash")
                if not snapshot_hash:
                    raise ScratchOrgException(
                        "Snapshot hashes must contain a snapshot_hash"
                    )
                snapshot_description = (
                    f" created from snapshot matched by hash {snapshot_hash}"
                )
                self.snapshot_name = snapshot_name
            if self.snapshot_name:
                if snapshot_description is None:
                    snapshot_description = (
                        f" created from snapshot {self.snapshot_name}"
                    )
                org_action_info["config"]["snapshot"] = self.snapshot_name
                with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                    json.dump(
                        config.to_snapshot_scratchdef(
                            snapshot=self.snapshot_name,
                            description=snapshot_description,
                        ),
                        f,
                    )
                    f.flush()
                    temp_file_path = os.path.abspath(f.name)
                try:
                    org_action_info["config"]["path"] = temp_file_path
                    yield temp_file_path
                finally:
                    os.unlink(temp_file_path)
            else:
                yield org_action_info["config"]["path"]

        def raise_error() -> NoReturn:
            message = f"{FAILED_TO_CREATE_SCRATCH_ORG}: \n{stdout}\n{stderr}"
            org_action_info["status"] = "error"
            try:
                output = json.loads(stdout)
                if (
                    output.get("message") == "The requested resource does not exist"
                    and output.get("name") == "NOT_FOUND"
                ):
                    exc = ScratchOrgException(
                        "The Salesforce CLI was unable to create a scratch org. Ensure you are connected using a valid API version on an active Dev Hub."
                    )
                    org_action_info["exception"] = str(exc)
                    self.add_action_to_history(
                        OrgCreateAction(
                            **org_action_info,
                        )
                    )
                    raise exc
            except json.decoder.JSONDecodeError as exc:
                raise ScratchOrgException(message) from exc

            exc = ScratchOrgException(message)
            org_action_info["exception"] = str(exc)
            self.add_action_to_history(
                OrgCreateAction(
                    **org_action_info,
                )
            )
            raise exc

        with temp_scratchdef_file() as config_file:
            args: List[str] = []
            if self.snapshot_name:
                args.extend(self._build_org_create_args(config_file))
            else:
                args.extend(self._build_org_create_args())
            extra_args = os.environ.get("SFDX_ORG_CREATE_ARGS", "")
            command = f"force:org:create --json {extra_args}"
            with open(config_file, "r") as f:
                self.logger.info(f.read())
            p: sarge.Command = sfdx(
                command=command,
                args=args,
                username=None,
                log_note="Creating scratch org",
            )
            stdout = p.stdout_text.read()
            stderr = p.stderr_text.read()

            org_action_info["sf_command"] = {
                "command": f"sf {command} {' '.join(args)}",
                "return_code": p.returncode,
                "output": stdout,
                "stderr": stderr,
            }
            result = {}  # for type checker.
            if p.returncode:
                raise_error()
            try:
                result = json.loads(stdout)

            except json.decoder.JSONDecodeError:
                raise_error()

            res = result.get("result")
            if not res or ("username" not in res) or ("orgId" not in res):
                raise_error()

            if res["username"] is None:
                raise ScratchOrgException(
                    "SFDX claimed to be successful but there was no username "
                    "in the output...maybe there was a gack?"
                )

            self.config["org_id"] = res["orgId"]
            self.config["username"] = res["username"]

            self.config["date_created"] = datetime.datetime.utcnow()

            self.logger.error(stderr)

            self.logger.info(
                f"Created: OrgId: {self.config['org_id']}, Username:{self.config['username']}"
            )

            scratch_org_info = res.get("ScratchOrgInfo", {})
            org_action_info["status"] = "success"
            org_action_info["org_id"] = self.org_id
            org_action_info["sfdx_alias"] = self.sfdx_alias
            org_action_info["username"] = self.username
            org_action_info["login_url"] = scratch_org_info.get("LoginUrl")
            org_action_info["instance"] = scratch_org_info.get("Instance")
            org_action_info["devhub"] = self.devhub
            self.add_action_to_history(
                OrgCreateAction(
                    **org_action_info,
                )
            )

        if self.config.get("set_password"):
            self.generate_password()

        # Flag that this org has been created
        self.config["created"] = True

    def _build_org_create_args(self, config_file_path: str | None = None) -> List[str]:
        create_args = ["-f", config_file_path or self.config_file, "-w", "120"]
        devhub_username: Optional[str] = self._choose_devhub_username()
        if devhub_username:
            create_args += ["--targetdevhubusername", devhub_username]
        if not self.namespaced:
            create_args += ["-n"]
        if self.noancestors:
            create_args += ["--noancestors"]
        if self.days:
            create_args += ["--durationdays", str(self.days)]
        if self.release:
            create_args += [f"release={self.release}"]
        if self.sfdx_alias:
            create_args += ["-a", self.sfdx_alias]
        with open(self.config_file, "r") as org_def:
            org_def_data = json.load(org_def)
            org_def_has_email = "adminEmail" in org_def_data
        if self.email_address and not org_def_has_email:
            create_args += [f"adminEmail={self.email_address}"]
        if self.default:
            create_args += ["-s"]
        if instance := self.instance or os.environ.get("SFDX_SIGNUP_INSTANCE"):
            create_args += [f"instance={instance}"]
        return create_args

    def _choose_devhub_username(self) -> Optional[str]:
        """Determine which devhub username to specify when calling sfdx, if any."""
        # If a devhub was specified via `cci org scratch`, use it.
        # (This will return None if "devhub" isn't set in the org config,
        # in which case sfdx will use its defaultdevhubusername.)
        devhub_username = self.devhub
        if not devhub_username and self.keychain is not None:
            # Otherwise see if one is configured via the "devhub" service
            try:
                devhub_service = self.keychain.get_service("devhub")
            except (ServiceNotConfigured, CumulusCIException):
                pass
            else:
                devhub_username = devhub_service.username
        return devhub_username

    def generate_password(self) -> None:
        """Generates an org password with: sfdx force:user:password:generate.
        On a non-zero return code, set the password_failed in our config
        and log the output (stdout/stderr) from sfdx."""

        if self.password_failed:
            self.logger.warning("Skipping resetting password since last attempt failed")
            return

        p: sarge.Command = sfdx(
            "force:user:password:generate",
            self.username,
            log_note="Generating scratch org user password",
        )

        if p.returncode:
            self.config["password_failed"] = True
            stderr = p.stderr_text.readlines()
            stdout = p.stdout_text.readlines()
            # Don't throw an exception because of failure creating the
            # password, just notify in a log message
            nl = "\n"  # fstrings can't contain backslashes
            self.logger.warning(
                f"Failed to set password: \n{nl.join(stdout)}\n{nl.join(stderr)}"
            )

    def format_org_days(self) -> str:
        if self.days_alive:
            org_days = f"{self.days_alive}/{self.days}"
        else:
            org_days = str(self.days)
        return org_days

    def can_delete(self) -> bool:
        return bool(self.date_created)

    def delete_org(self) -> None:
        """Uses sfdx force:org:delete to delete the org"""
        if not self.created:
            self.logger.info("Skipping org deletion: the scratch org does not exist.")
            return

        org_action_info = {
            "timestamp": datetime.datetime.now().timestamp(),
            "action_type": "OrgDelete",
            "org_id": self.config["org_id"],
            "repo": self.keychain.project_config.repo_url,
            "branch": self.keychain.project_config.repo_branch,
            "commit": self.keychain.project_config.repo_commit,
            "log": "",
        }

        command = "force:org:delete -p"
        org_action_info["sf_command"] = {
            "command": f"sf {command}",
        }
        p: sarge.Command = sfdx(command, self.username, "Deleting scratch org")
        stdout = p.stdout_text.readlines()
        stderr = p.stderr_text.readlines()
        sfdx_output: List[str] = stdout + stderr
        org_action_info["sf_command"]["return_code"] = p.returncode
        org_action_info["sf_command"]["output"] = "\n".join(stdout)
        org_action_info["sf_command"]["stderr"] = "\n".join(stderr)

        for line in sfdx_output:
            if "error" in line.lower():
                self.logger.error(line)
            else:
                self.logger.info(line)

        if p.returncode:
            message = "Failed to delete scratch org"
            exc = ScratchOrgException(message)
            org_action_info["status"] = "error"
            org_action_info["exception"] = str(exc)
            # Record the failed action to the org's history
            self.add_action_to_history(
                OrgDeleteAction(
                    **org_action_info,
                )
            )
            raise exc

        # Record the action to the org's history
        org_action_info["status"] = "success"
        self.add_action_to_history(OrgDeleteAction(**org_action_info))

        # Flag that this org has been deleted
        self.config["created"] = False
        self.config["username"] = None
        self.config["date_created"] = None
        self.config["instance_url"] = None
        self.save()
