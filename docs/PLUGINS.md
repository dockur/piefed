# Plugins

PieFed includes a simple plugin engine so third parties can extend PieFed functionality without adding their code to the
main PieFed project. All plugins run within PieFed and therefore must be licenced under an AGPL 3.0-compatible licence and their source code
publicly available.

Each plugin is a directory under app/plugins and must include a __init__.py file. In that file there must be a plugin_info()
function.

See app/plugins/example_plugin/* to get started. Also check out [!piefed_plugins@piefed.social](https://piefed.social/c/piefed_plugins) for more plugin discussion, ask questions, and browse examples.

Plugins can have their code executed by adding a @hook decorator to a function. The current list of hooks are:

 - `before_post_create` - as the name suggests, this is run before a post is created. The contents of the post is passed in through the parameters.
 Modify the data as you wish and `return` it.

  - `after_post_create` - this is run after a post is created and the `Post` object is passed as a parameter.

  - `ban_user` - when a user is banned. The `User` object is passed as a parameter.

  - `cron_often` - is run at a periodic interval. The frequency is determined by how often the send_queue runs. The cron hooks are executed from a CLI task and do not run in a request context.

  - `cron_daily` - is run once a day in the daily_maintenance_task.

  - `new_user` - is run when a new user is verified; meaning that they have completed any required email verification and their application has been accepted (if applicable). The newly created `User` object is passed to this hook as an argument.

  - `new_registration_for_approval` - is run when a user has submitted an application and (if applicable) verified their email, but the application is awaiting admin approval. The `UserRegistration` object is passed to this plugin as an argument.

  - `new_local_community` - is run whenever a new local community is created on the instance and the `Community` object is passed as a parameter

  - `new_remote_community` - is run whenever a new remote community is federated to the instance and the `Community` object is passed as a parameter

  - `webhook` - is run whenever a `POST` is received at the `/webhook` route. The payload from the webhook is passed to the plugin. Validation and authentication of the payload needs to be done by the plugin as flask just passes along the payload directly. Plugins making use of this hook should return the payload unchanged so as not to interfere with other plugins that might make use of this hook.

More hooks will be added over time, presently the plugin engine is still experimental and undergoing heavy development.
