"""
Plugin hook system
"""
from functools import wraps
from typing import Dict, List, Callable, Any
import logging

logger = logging.getLogger(__name__)

# Global hook registry
_hooks: Dict[str, List[Callable]] = {}
# Track which plugin registered which hooks
_plugin_hooks: Dict[str, Dict[str, List[str]]] = {}


def hook(hook_name: str):
    """
    Decorator to register a function as a hook handler
    
    Usage:
        @hook("before_post_create")
        def my_handler(data):
            # Handle the hook
            return data
    """
    def decorator(func: Callable):
        if hook_name not in _hooks:
            _hooks[hook_name] = []
        
        _hooks[hook_name].append(func)
        logger.info(f"Registered hook '{hook_name}' -> {func.__name__}")
        
        # Try to determine which plugin this hook belongs to from the function's module
        try:
            module_name = func.__module__
            logger.debug(f"Hook function {func.__name__} belongs to module: {module_name}")
            # Extract plugin name from module path like 'app.plugins.example_plugin'
            if module_name and module_name.startswith('app.plugins.') and module_name.count('.') >= 2:
                plugin_name = module_name.split('.')[2]
                register_plugin_hook(plugin_name, hook_name, func.__name__)
                logger.info(f"Auto-registered hook {hook_name} -> {func.__name__} for plugin {plugin_name}")
        except Exception as e:
            logger.debug(f"Could not determine plugin for hook {hook_name}: {e}")
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


def fire_hook(hook_name: str, data: Any = None, **kwargs) -> Any:
    """
    Fire a hook and call all registered handlers in alphabetical order by function name
    
    Args:
        hook_name: Name of the hook to fire
        data: Data to pass to hook handlers
        **kwargs: Additional keyword arguments
    
    Returns:
        Modified data after all handlers have processed it
    """
    if hook_name not in _hooks:
        return data
    
    # Sort handlers alphabetically by function name
    sorted_handlers = sorted(_hooks[hook_name], key=lambda func: func.__name__)
    
    logger.debug(f"Firing hook '{hook_name}' with {len(sorted_handlers)} handlers")
    
    result = data
    for handler in sorted_handlers:
        try:
            result = handler(result, **kwargs)
        except Exception as e:
            logger.error(f"Error in hook handler {handler.__name__}: {e}")
    
    return result


def get_registered_hooks() -> Dict[str, List[str]]:
    """Get all registered hooks and their handlers (sorted alphabetically)"""
    return {
        hook_name: sorted([handler.__name__ for handler in handlers])
        for hook_name, handlers in _hooks.items()
    }


def get_plugin_hooks() -> Dict[str, Dict[str, List[str]]]:
    """Get hooks grouped by plugin"""
    return _plugin_hooks.copy()


def register_plugin_hook(plugin_name: str, hook_name: str, function_name: str):
    """Register a hook for a specific plugin"""
    if plugin_name not in _plugin_hooks:
        _plugin_hooks[plugin_name] = {}
    if hook_name not in _plugin_hooks[plugin_name]:
        _plugin_hooks[plugin_name][hook_name] = []
    _plugin_hooks[plugin_name][hook_name].append(function_name)


def clear_hooks():
    """Clear all registered hooks (useful for testing)"""
    global _hooks, _plugin_hooks
    _hooks.clear()
    _plugin_hooks.clear()