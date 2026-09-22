"""Слой приложений: каталог, реестр, алиасы, обнаружение и запуск."""
from .aliases import AliasResolver, normalize, similarity, stem, translit
from .catalog import APPS, AppSpec, builtin_aliases, spec, specs_for_discovery
from .registry import AppRecord, AppRegistry, LookupResult
from .discovery import AppDiscovery
from .launcher import AppLauncher, LaunchResult, OSLaunchEnv, FakeLaunchEnv

__all__ = ["AliasResolver", "normalize", "similarity", "stem", "translit",
           "APPS", "AppSpec", "builtin_aliases", "spec", "specs_for_discovery",
           "AppRecord", "AppRegistry", "LookupResult", "AppDiscovery",
           "AppLauncher", "LaunchResult", "OSLaunchEnv", "FakeLaunchEnv"]
