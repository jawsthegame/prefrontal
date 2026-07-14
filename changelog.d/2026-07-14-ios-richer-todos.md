- **iOS: richer todo management** ✅ — the Todos tab gains two things the web had
  and the app didn't. **Edit** on any todo now opens a sheet to move/clear its
  **deadline** (a date picker, sent as an offset-aware timestamp so the server
  times it right) and set/clear its **notes** (which ride along on the todo's
  nudges); notes also render inline on the row. And a new **Stuck & avoided**
  review screen (toolbar) surfaces honest prioritization: the important todos
  you keep **skipping** (`GET /todos/avoided`, worst first, each startable in
  place) and the tasks you keep **bailing on** (`GET /todos/stuck`) with the
  Task-Paralysis body-double nudge and a tiny first step you can add as a todo.
  New `EditTodoSheet` + `StuckAvoidedView`, `StuckTodo`/`AvoidedTodo` models, and
  `APIClient.setTodoDeadline` / `.setTodoNotes` / `.stuckTodos` / `.avoidedTodos`.
