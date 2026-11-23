# Starter templates for `scaffold_module`

`scaffold_module(path, role, purpose, patterns)` renders `<role><ext>.tmpl` (falling back to
`default<ext>.tmpl`) and writes the module header for you; `test<ext>.tmpl` becomes the linked
test stub. `get_conventions(<role>)` points at the matching template while the project has no
real examples yet.

Placeholders (Python `str.format`, so write literal braces as `{{` / `}}`):
`{header}`, `{class_name}`, `{func_name}`, `{module}`, `{stem}`; test templates also get
`{first_symbol}` and `{import_path}`.

Edit these to match your project's real conventions; they are your day-0 examples.
