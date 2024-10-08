import sys
from abc import abstractmethod
from typing import List, Optional, Type

from cumulusci.core.config import BaseProjectConfig, UniversalConfig
from cumulusci.core.debug import DebugMode, get_debug_mode
from cumulusci.core.exceptions import NotInProject, ProjectConfigNotFound
from cumulusci.core.flowrunner import FlowCallback, FlowCoordinator
from cumulusci.core.keychain import BaseProjectKeychain


# pylint: disable=assignment-from-none
class BaseCumulusCI:
    universal_config_class = UniversalConfig
    project_config_class = BaseProjectConfig
    keychain_class = BaseProjectKeychain
    callback_class = FlowCallback

    universal_config: UniversalConfig
    project_config: Optional[BaseProjectConfig]
    keychain: Optional[BaseProjectKeychain]
    debug_mode: DebugMode
    project_config_error: Exception

    def __init__(self, *args, load_keychain=True, **kwargs):
        self.keychain = None
        self.debug_mode = get_debug_mode()

        self._load_universal_config()

        try:
            self._load_project_config(*args, **kwargs)
            self._add_repo_to_path()
        except (NotInProject, ProjectConfigNotFound) as e:
            self.project_config = None
            self.project_config_error = e
        if load_keychain:
            self._load_keychain()

    @property
    def universal_config_cls(self) -> Type:
        klass = self.get_universal_config_class()
        return klass or self.universal_config_class

    @abstractmethod
    def get_universal_config_class(self) -> Optional[Type]:
        return None

    @property
    def project_config_cls(self) -> Type:
        klass = self.get_project_config_class()
        return klass or self.project_config_class

    @abstractmethod
    def get_project_config_class(self) -> Optional[Type]:
        return None

    @property
    def keychain_cls(self) -> Type:
        klass = self.get_keychain_class()
        return klass or self.keychain_class

    @abstractmethod
    def get_keychain_class(self) -> Optional[Type]:
        return None

    @property
    def keychain_key(self):
        return self.get_keychain_key()

    @abstractmethod
    def get_keychain_key(self):
        return None

    def _add_repo_to_path(self):
        if self.project_config and self.project_config.repo_root:
            sys.path.append(self.project_config.repo_root)

    def _load_universal_config(self):
        self.universal_config = self.universal_config_cls()

    def _load_project_config(self, *args, **kwargs):
        self.project_config = self.project_config_cls(
            self.universal_config, *args, **kwargs
        )
        if self.project_config is not None:
            self.project_config._add_tasks_directory_to_python_path()

    def _load_keychain(self):
        if self.keychain is not None:
            return

        keychain_key = self.keychain_key if self.keychain_cls.encrypted else None

        if self.project_config is None:
            self.keychain = self.keychain_cls(self.universal_config, keychain_key)
        else:
            self.keychain = self.keychain_cls(self.project_config, keychain_key)
            self.project_config.keychain = self.keychain

    def get_flow(
        self,
        name: str,
        options: Optional[dict] = None,
        skip: Optional[List[str]] = None,
        skip_from: Optional[str] = None,
        start_from: Optional[str] = None,
    ) -> FlowCoordinator:
        """Get a primed and ready-to-go flow coordinator."""
        if not self.project_config:
            raise ProjectConfigNotFound
        flow_config = self.project_config.get_flow(name)
        callbacks = self.callback_class()
        coordinator = FlowCoordinator(
            flow_config.project_config,
            flow_config,
            name=flow_config.name,
            options=options,
            skip=skip or flow_config.skip,
            skip_from=skip_from or flow_config.skip_from,
            start_from=start_from or flow_config.start_from,
            callbacks=callbacks,
        )
        return coordinator
