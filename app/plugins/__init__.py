"""
Plugin system for PyFedi
"""
import os
import importlib
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, List, Any
import logging

from .hooks import fire_hook, get_registered_hooks, register_plugin_hook, get_plugin_hooks

logger = logging.getLogger(__name__)

# Global plugin registry
_loaded_plugins: Dict[str, Any] = {}


def load_plugins(plugins_dir: str = None) -> Dict[str, Any]:
    """
    Load all plugins from the plugins directory
    
    Args:
        plugins_dir: Directory containing plugins (defaults to app/plugins)
    
    Returns:
        Dictionary of loaded plugins
    """
    if plugins_dir is None:
        plugins_dir = os.path.join(os.path.dirname(__file__))
    
    plugins_path = Path(plugins_dir)
    if not plugins_path.exists():
        logger.warning(f"Plugins directory not found: {plugins_dir}")
        return {}
    
    loaded_count = 0
    
    for plugin_dir in plugins_path.iterdir():
        if not plugin_dir.is_dir() or plugin_dir.name.startswith('_'):
            continue
            
        plugin_name = plugin_dir.name
        plugin_file = plugin_dir / "__init__.py"
        
        if not plugin_file.exists():
            logger.warning(f"Plugin {plugin_name} missing __init__.py")
            continue
        
        try:
            # Load the plugin module
            spec = importlib.util.spec_from_file_location(
                f"app.plugins.{plugin_name}", 
                plugin_file
            )
            if spec is None or spec.loader is None:
                logger.error(f"Could not load plugin spec for {plugin_name}")
                continue
                
            plugin_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin_module)
            
            # Get plugin info if available
            plugin_info = {}
            if hasattr(plugin_module, 'plugin_info'):
                plugin_info = plugin_module.plugin_info()
            
            _loaded_plugins[plugin_name] = {
                'module': plugin_module,
                'info': plugin_info,
                'path': str(plugin_dir)
            }
            
            loaded_count += 1
            logger.info(f"Loaded plugin: {plugin_name}")
            
        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_name}: {e}")
    
    logger.info(f"Successfully loaded {loaded_count} plugins")
    return _loaded_plugins


def get_loaded_plugins() -> Dict[str, Any]:
    """Get all currently loaded plugins"""
    return _loaded_plugins.copy()


def get_plugin_info(plugin_name: str) -> Dict[str, Any]:
    """Get information about a specific plugin"""
    if plugin_name in _loaded_plugins:
        return _loaded_plugins[plugin_name]['info']
    return {}



def reload_plugin(plugin_name: str) -> bool:
    """Reload a specific plugin"""
    if plugin_name not in _loaded_plugins:
        return False
    
    try:
        plugin_path = _loaded_plugins[plugin_name]['path']
        
        # Clean up old hooks for this plugin
        from .hooks import _hooks, _plugin_hooks
        if plugin_name in _plugin_hooks:
            # Remove old hook registrations
            for hook_name, function_names in _plugin_hooks[plugin_name].items():
                if hook_name in _hooks:
                    # Remove functions that belong to this plugin
                    _hooks[hook_name] = [
                        func for func in _hooks[hook_name] 
                        if func.__module__ != f"app.plugins.{plugin_name}"
                    ]
                    # Clean up empty hook lists
                    if not _hooks[hook_name]:
                        del _hooks[hook_name]
            # Clear plugin hook registry
            del _plugin_hooks[plugin_name]
        
        # Remove from loaded plugins
        del _loaded_plugins[plugin_name]
        
        # Reload
        plugin_dir = Path(plugin_path)
        plugin_file = plugin_dir / "__init__.py"
        
        spec = importlib.util.spec_from_file_location(
            f"app.plugins.{plugin_name}", 
            plugin_file
        )
        if spec is None or spec.loader is None:
            return False
            
        plugin_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin_module)
        
        plugin_info = {}
        if hasattr(plugin_module, 'plugin_info'):
            plugin_info = plugin_module.plugin_info()
        
        _loaded_plugins[plugin_name] = {
            'module': plugin_module,
            'info': plugin_info,
            'path': str(plugin_dir)
        }
        
        logger.info(f"Reloaded plugin: {plugin_name}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to reload plugin {plugin_name}: {e}")
        return False


# Export the hook system
__all__ = [
    'load_plugins', 
    'get_loaded_plugins', 
    'get_plugin_info', 
    'reload_plugin',
    'fire_hook',
    'get_registered_hooks',
    'get_plugin_hooks'
]