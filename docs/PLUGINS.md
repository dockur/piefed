# Plugins

PieFed includes a simple plugin engine so third parties can extend PieFed functionality without adding their code to the
main PieFed project. All plugins run within PieFed and therefore must be licenced under an AGPL 3.0-compatible licence and their source code
publicly available.

Each plugin is a directory under app/plugins and must include a __init__.py file. In that file there must be a plugin_info()
function.

See app/plugins/example_plugin/* to get started.

Plugins can have their code executed by adding a @hook decorator to a function. The current list of hooks are:

 - `before_post_create` - as the name suggests, this is run before a post is created. The contents of the post is passed in through the parameters.
 Modify the data as you wish and `return` it.

  - `after_post_create` - take a guess.

More hooks will be added over time, presently the plugin engine is still experimental and undergoind heavy development.
