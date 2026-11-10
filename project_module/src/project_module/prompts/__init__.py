"""Loader for the prompt files shipped in this package directory."""

import re
from collections.abc import Mapping, Sequence
from importlib.resources import files
from string import Template
from typing import Final

# A prompt file may open with an HTML comment crediting its source; it is not sent to the model.
_ATTRIBUTION: Final = re.compile(r"\A<!--.*?-->\s*", re.DOTALL)


def load_prompt(name: str) -> str:
    """Return the text of `prompts/<name>` without its attribution comment.

    Raises
    ------
    FileNotFoundError
        If the prompt file is not shipped with the package.
    """
    text = files(__name__).joinpath(name).read_text(encoding="utf-8")
    return _ATTRIBUTION.sub("", text).strip()


def render_prompt(prompt_files: Sequence[str], values: Mapping[str, object]) -> str:
    """Join the prompt files in order and fill their `$name` placeholders from `values`.

    Raises
    ------
    FileNotFoundError
        If a prompt file is not shipped with the package.
    KeyError
        If a placeholder has no value.
    ValueError
        If a prompt contains a `$` that does not start a placeholder.
    """
    joined = "\n\n".join(load_prompt(name) for name in prompt_files)
    return Template(joined).substitute(values)
