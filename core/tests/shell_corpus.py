"""Command texts for the shell-split conformance test (TD-5156).

Every text is built only from the builtins ``:``, ``true``, ``eval``,
``wait``, ``shopt`` and ``declare``, shell keywords, functions it defines,
and atoms of the form
``echo m<k> >>o``: each atom appends its own marker to the file ``o`` in a
throwaway folder, so running a text shows which atoms a shell ran, and with
which arguments, whatever pipes, substitutions or background jobs sit around
them. Nothing here names another program, a network host or a path outside
that folder.

The atoms are combined with the constructs that decide where a shell ends a
command: newlines, ``\\``-newline, comments, here-documents, quotes that span
lines, ``&``, ``|&``, ``;;``, ``()``, ``{}``, ``$(…)``, backticks, carriage
returns and unusual whitespace, and the bash-only words that hold ``(``,
``#`` or ``<<`` as text (``${ cmd; }``, ``[[ … =~ ( ) ]]``, extglob,
arrays and subscripts, ``$[ ]``). Each base text is used alone, inside every
wrapper, and in a fixed sample of pairs. A line no shell can run as a
command (``2]=x``) names no program on any host.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

#: name → argv prefix; the text is the next argument.
SHELLS: dict[str, tuple[str, ...]] = {
    "bash": ("bash", "-c"),
    "bash --posix": ("bash", "--posix", "-c"),
    "dash": ("dash", "-c"),
    "sh": ("sh", "-c"),
    # The default autonomy image (alpine) runs busybox ash as /bin/sh.
    "busybox sh": ("busybox", "sh", "-c"),
}
_SHELL_ERROR = re.compile(r"(?:ba|da)?sh: ")


def available_shells() -> dict[str, tuple[str, ...]]:
    return {name: argv for name, argv in SHELLS.items() if shutil.which(argv[0])}


class Text:
    """Hands out atoms with markers unique within one text."""

    def __init__(self) -> None:
        self.markers: list[str] = []
        self.functions = 0

    def a(self, form: str = "echo {m} >>o") -> str:
        marker = f"m{len(self.markers) + 1}"
        self.markers.append(marker)
        return form.format(m=marker)

    def fn(self, form: str) -> str:
        """A function defined and called by *form*, under a name no other
        piece uses: a piece that redefined a caller's function inside a
        pipeline would call its caller again, for ever."""
        self.functions += 1
        return form.format(n=f"f{self.functions}", a=self.a())


Piece = Callable[[Text], str]

# fmt: off
BASE: list[Piece] = [
    # Separators and line ends.
    lambda t: t.a(),
    lambda t: f"{t.a()}; {t.a()}",
    lambda t: f"{t.a()}\n{t.a()}",
    lambda t: f"{t.a()} && {t.a()}",
    lambda t: f"{t.a()} || {t.a()}",
    lambda t: f"{t.a()} & {t.a()}",
    lambda t: f"{t.a()} | {t.a()}",
    lambda t: f"{t.a()} |& {t.a()}",
    lambda t: f"{t.a()};; {t.a()}",
    lambda t: f"{t.a()};& {t.a()}",
    lambda t: f"{t.a()} &> o2; {t.a()}",
    lambda t: f"{t.a()}\r\n{t.a()}",
    lambda t: f"{t.a()}\r;{t.a()}",
    lambda t: f"\t{t.a()}\t;\t{t.a()}",
    lambda t: f"{t.a()}\v; {t.a()}",
    lambda t: f"{t.a()}\f\n{t.a()}",
    lambda t: f"{t.a()}\u00a0; {t.a()}",
    lambda t: f"{t.a()}\u2028{t.a()}",
    lambda t: f"\n\n{t.a()}\n\n",
    # Backslash-newline.
    lambda t: f"{t.a()} \\\n{t.a()}",
    lambda t: f"{t.a()}\\\n; {t.a()}",
    lambda t: f"{t.a()};\\\n;{t.a()}",
    lambda t: f"{t.a()} &\\\n& {t.a()}",
    lambda t: f"{t.a()} |\\\n| {t.a()}",
    lambda t: f"{t.a()} |\\\n& {t.a()}",
    lambda t: f"{t.a()} &\\\n> o2; {t.a()}",
    lambda t: t.a("ec\\\nho {m} >>o"),
    lambda t: t.a("echo {m} >\\\n>o"),
    lambda t: t.a("echo {m} \\\n\\\n>>o"),
    lambda t: f"\\\n{t.a()}",
    lambda t: f"{t.a()} \\",
    lambda t: f"(\\\n({t.a()}))",
    lambda t: f"true \\\n&& {t.a()}",
    lambda t: f"{t.a()} \\\r\n{t.a()}",
    # Grouping and compound commands.
    lambda t: f"({t.a()})",
    lambda t: f"({t.a()}\n{t.a()})",
    lambda t: f"{{ {t.a()}; }}",
    lambda t: f"{{ {t.a()}\n}}",
    lambda t: f"( ({t.a()}) )",
    lambda t: f"(({t.a()}))",
    lambda t: t.fn("{n}() {{ {a}; }}; {n}"),
    lambda t: t.fn("{n}() {{\n{a}\n}}\n{n}"),
    lambda t: t.fn("{n}() ( {a} )\n{n}"),
    lambda t: f"if true; then {t.a()}; fi",
    lambda t: f"if true\nthen {t.a()}\nelse {t.a()}\nfi",
    lambda t: f"while false; do {t.a()}; done; {t.a()}",
    lambda t: f"for x in 1; do {t.a()}; done",
    lambda t: f"for x in 1\ndo\n{t.a()}\ndone",
    lambda t: f"case x in x) {t.a()};; esac",
    lambda t: f"case x in\n(x) {t.a()}\n;;\nesac\n{t.a()}",
    lambda t: f"case x in x) {t.a()};& y) {t.a()};; esac",
    lambda t: f"ca\\\nse x in x) {t.a()};; esac",
    lambda t: f"! {t.a()}",
    # Substitutions.
    lambda t: f": $({t.a()})",
    lambda t: f': "$({t.a()})"',
    lambda t: f": `{t.a()}`",
    lambda t: f': "`{t.a()}`"',
    lambda t: f": $({t.a()}\n{t.a()})",
    lambda t: f": $(\n{t.a()}\n)",
    lambda t: f": `\n{t.a()}\n`",
    lambda t: f": $(: `{t.a()}`)",
    lambda t: f": $( ({t.a()}) )",
    lambda t: f": $( # )\n{t.a()}\n)",
    lambda t: f": $(: ')'; {t.a()})",
    lambda t: f': $(: ")"; {t.a()})',
    lambda t: f": $(case x in x) {t.a()};; esac)",
    lambda t: f": $(ca\\\nse x in x) {t.a()};; esac)",
    lambda t: f": <({t.a()})",
    lambda t: f": >({t.a()})",
    lambda t: f": ${{x:-$({t.a()})}}",
    lambda t: f': "${{x:-$({t.a()})}}"',
    lambda t: f": ${{x:-\n}}; {t.a()}",
    lambda t: f": $((1)); {t.a()}",
    lambda t: f": $\\\n({t.a()})",
    lambda t: f': "$\\\n({t.a()})"',
    lambda t: f": $\\\n{{x:-$({t.a()})}}",
    lambda t: f": `: \\`{t.a()}\\``",
    lambda t: f"eval '{t.a()}'",
    lambda t: f'eval "{t.a()}\n{t.a()}"',
    lambda t: f"eval {t.a()}",
    # Here-documents.
    lambda t: f": <<E\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<'E'\n{t.a()}\nE\n{t.a()}",
    lambda t: f': <<"E"\n{t.a()}\nE\n{t.a()}',
    lambda t: f": <<\\E\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<-E\n\t{t.a()}\n\tE\n{t.a()}",
    lambda t: f": << E\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E; {t.a()}\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E <<F\n{t.a()}\nE\n{t.a()}\nF\n{t.a()}",
    lambda t: f": <<E\n$({t.a()})\nE\n{t.a()}",
    lambda t: f": <<'E'\n$({t.a()})\nE\n{t.a()}",
    lambda t: f": <<E\n`{t.a()}`\nE",
    lambda t: f": <<E\nx\\\nE\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E\nE\\\n\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<'E'\nE\\\n\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E\n$(\n{t.a()}\n)\nE\n{t.a()}",
    lambda t: f": <<E\n$(: \\\nE\n{t.a()})\nE\n{t.a()}",
    lambda t: f": <<\\\nE\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E\\\nF\n{t.a()}\nEF\n{t.a()}",
    lambda t: f": <<E\\\n\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<\\\n-E\n\t{t.a()}\n\tE\n{t.a()}",
    lambda t: f": $(: <<E) ; {t.a()}\n{t.a()}\nE\n{t.a()}",
    lambda t: f": $(: <<E\n{t.a()}\nE\n); {t.a()}",
    lambda t: f": $(: <<E\n)\nE\n); {t.a()}",
    lambda t: f": $(: <<E\n{t.a()}\nE); {t.a()}\nE\n{t.a()}",
    lambda t: f": `: <<E`; {t.a()}\n{t.a()}\nE\n{t.a()}",
    lambda t: f": `: <<E\n{t.a()}\nE\n`; {t.a()}",
    lambda t: f': "$(: <<E)"; {t.a()}\n{t.a()}\nE\n{t.a()}',
    lambda t: f": <<E $(\n{t.a()}\n)\n{t.a()}\nE\n{t.a()}",
    lambda t: f': <<E "\n{t.a()}\n"\n{t.a()}\nE\n{t.a()}',
    lambda t: f": <<E '\n{t.a()}\n'\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E `\n{t.a()}\n`\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E ${{x:-\n{t.a()}\n}}\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E # '\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E\n{t.a()}\nE",
    lambda t: f"{t.a()}; : <<E",
    lambda t: f": <<E\n{t.a()}",
    lambda t: f": <<E\r\n{t.a()}\r\nE\r\n{t.a()}",
    lambda t: f": <<E\n{t.a()}\nE \n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E|{t.a()}\n{t.a()}\nE\n{t.a()}",
    lambda t: f"(: <<E\n{t.a()}\nE\n)\n{t.a()}",
    lambda t: f"(: <<E)\n{t.a()}\nE\n{t.a()}",
    lambda t: f"{{ : <<E; }}\n{t.a()}\nE\n{t.a()}",
    lambda t: t.fn("{n}() {{ : <<E; }}\n{a}\nE") + f"\n{t.a()}",
    lambda t: f": <<<{t.a()}\n{t.a()}",
    lambda t: f': <<< "x\n{t.a()}"\n{t.a()}',
    lambda t: f": <\\\n<E\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<\\\n<x\n{t.a()}",
    lambda t: f": <<E$x\n{t.a()}\nE\n{t.a()}",
    lambda t: f": <<E\n{t.a()}\nE\n: <<E\n{t.a()}\nE\n{t.a()}",
    lambda t: f"eval ': <<E'\n{t.a()}\nE\n{t.a()}",
    lambda t: f"eval ': <<E\n{t.a()}\nE'\n{t.a()}",
    # Quotes that span lines.
    lambda t: f": '\n{t.a()}\n'; {t.a()}",
    lambda t: f': "\n{t.a()}\n"; {t.a()}',
    lambda t: f": $'\n{t.a()}\n'; {t.a()}",
    lambda t: f': "\\\n{t.a()}"; {t.a()}',
    lambda t: f": '\\\n'; {t.a()}",
    lambda t: f": $'\\'' ; {t.a()} ; : '",
    lambda t: f": $\\\n'a\\'b' ; {t.a()} ; : 'c'",
    lambda t: f': $"\n{t.a()}\n"; {t.a()}',
    # Comments.
    lambda t: f"# {t.a()}\n{t.a()}",
    lambda t: f"{t.a()} # {t.a()}",
    lambda t: f"{t.a()} #\\\n{t.a()}",
    lambda t: f": #'\n{t.a()}\n'",
    lambda t: f": a#b; {t.a()}",
    lambda t: f": \\\n# '\n{t.a()}\n'",
    lambda t: f": \\\n#{t.a()}\n{t.a()}",
    lambda t: f"{t.a()};# {t.a()}\n{t.a()}",
    lambda t: f": $(# \n{t.a()}\n)",
    lambda t: f": `# \n{t.a()}`",
    lambda t: f": ${{x#'}}; {t.a()}; : '",
    lambda t: f"#!\\\n{t.a()}",
    lambda t: f"{t.a()}\r# '\n{t.a()}\n'",
    lambda t: f"# <<E\n{t.a()}",
    # Escapes and odd delimiters.
    lambda t: f"{t.a()} \\; {t.a()}",
    lambda t: f"{t.a()} \\& {t.a()}",
    lambda t: f"{t.a()} \\| {t.a()}",
    lambda t: f"{t.a()} \\# {t.a()}",
    lambda t: f"{t.a()} \\' ; {t.a()} '",
    lambda t: f": \\$({t.a()})",
    lambda t: f": \\`{t.a()}`",
    lambda t: f': "a\'b; {t.a()}"; {t.a()}',
    lambda t: f": 'a\"b; {t.a()}'; {t.a()}",
    lambda t: f": <<E.F\n{t.a()}\nE.F\n{t.a()}",
    lambda t: f": <<'E F'\n{t.a()}\nE F\n{t.a()}",
    lambda t: f": <<E'F'\n{t.a()}\nEF\n{t.a()}",
    lambda t: f": <<-'E'\n\t{t.a()}\n\tE\n{t.a()}",
    lambda t: f": <<E\r\n{t.a()}\nE\n{t.a()}\nE\r\n{t.a()}",
    lambda t: f": <<E | :\n{t.a()}\nE\n{t.a()}",
    lambda t: f"if true; then : <<E\n{t.a()}\nE\nfi; {t.a()}",
    lambda t: f"case x in x) : <<E\n{t.a()}\nE\n;; esac; {t.a()}",
    lambda t: f": <<E\n$(\nE\n{t.a()}\n)\nE\n{t.a()}",
    lambda t: f": <<E\n\\$({t.a()})\nE\n{t.a()}",
    lambda t: f": <<E\n'$({t.a()})'\nE",
    lambda t: f": `: $({t.a()})`",
    lambda t: f": $(: ')' # )\n{t.a()})",
    lambda t: f": $(: <<'E'\n)\nE\n{t.a()})",
    lambda t: f"{{ {t.a()};}}",
    lambda t: f"coproc {t.a()}",
    lambda t: f"time {t.a()}",
    lambda t: f"{t.a()} &>> o; {t.a()}",
    lambda t: f"{t.a()} >| o2; {t.a()}",
    lambda t: f"{t.a()} <> o2; {t.a()}",
    lambda t: f"{t.a()} >&-; {t.a()}",
    lambda t: f"{t.a()} &; {t.a()}",
    lambda t: f"; {t.a()}",
    lambda t: f": '\\\n{t.a()}'",
    lambda t: f': "\\\n{t.a()}"',
    lambda t: f"{t.a()}\u3000; {t.a()}",
    lambda t: f"{t.a()}\u2003\n{t.a()}",
    # bash-only words that hold `(`, `#`, `<<` or a command as text.
    lambda t: f": ${{ {t.a()}; }}",
    lambda t: f": ${{| {t.a()}; }}",
    lambda t: f': "${{ {t.a()}; }}"',
    lambda t: f": ${{\n{t.a()}\n}}",
    lambda t: f": ${{\t{t.a()};}}",
    lambda t: f": ${{x:-{{}}; {t.a()}; : }}",
    lambda t: f"[[ x =~ ( #) ]]; {t.a()}",
    lambda t: f"[[ x == @( #) ]]; {t.a()}",
    lambda t: f"[[ ( #x ) ]]; {t.a()}",
    lambda t: f"[[ x ]] && {t.a()}",
    lambda t: f"shopt -s extglob\n: @( #) ; {t.a()}",
    lambda t: f"shopt -s extglob\n: !( #) ; {t.a()}",
    lambda t: f"shopt -s extglob\n: @()#x; {t.a()}",
    lambda t: f"x=@( #); {t.a()}",
    lambda t: f"a=( #) ; {t.a()}\n)",
    lambda t: f"a=([1<<true]=x)\n{t.a()}\ntrue]=x)\n{t.a()}",
    lambda t: f"a[1<<true]=x\n{t.a()}\ntrue]=x\n{t.a()}",
    lambda t: f"declare -A a\na[x #]=y; {t.a()}",
    lambda t: f"a[1]=x; {t.a()}",
    lambda t: f": $[ 1 <<true ]\n{t.a()}\ntrue\n{t.a()}",
    lambda t: f"coproc N {{ {t.a()}; }}; wait",
    lambda t: f"coproc N ( {t.a()} ); wait",
    lambda t: t.a("{{echo,{m}}} >>o"),
    lambda t: t.a("echo {m} {{a,b}} >>o"),
    # An echo whose arguments hold an expansion, next to other atoms.
    lambda t: t.a("echo {m} $(:) >>o") + f"; {t.a()}",
    lambda t: t.a('echo "$x" {m} >>o') + f"\n{t.a()}",
]
# fmt: on

# Wrappers place a base text inside a bigger one.
WRAPPERS: list[Callable[[Text, str], str]] = [
    lambda t, f: f"({f})",
    lambda t, f: f"{{ {f}\n}}",
    lambda t, f: f": $({f}\n)",
    lambda t, f: f': "$({f}\n)"',
    lambda t, f: f"{f}\n{t.a()}",
    lambda t, f: f"{t.a()}; {f}",
    lambda t, f: f"if true; then\n{f}\nfi",
    lambda t, f: t.fn("{n}() {{\n" + f.replace("{", "{{").replace("}", "}}") + "\n}}; {n}"),
    lambda t, f: f": <<E\n{f}\nE\n{t.a()}",
    lambda t, f: f": <<'E'\n{f}\nE\n{t.a()}",
    lambda t, f: f"{f} \\\n{t.a()}",
    lambda t, f: f"{f}\n# '\n{t.a()}",
    lambda t, f: f": <<E\n$({f}\n)\nE\n{t.a()}",
    lambda t, f: f"case x in x) {f}\n;; esac",
    lambda t, f: f"{f} &",
    lambda t, f: f": ${{x:-$({f}\n)}}",
]

SEPARATORS = ["; ", "\n", " && ", " | ", " & ", " \\\n", "\r\n", " #\n"]


def corpus(pairs: int = 600) -> list[str]:
    """Every base text, alone and in every wrapper, and a fixed sample of pairs."""
    texts = [piece(Text()) for piece in BASE]
    for wrap in WRAPPERS:
        for piece in BASE:
            t = Text()
            texts.append(wrap(t, piece(t)))
    rng = random.Random(5156)
    for _ in range(pairs):
        first, second = rng.choice(BASE), rng.choice(BASE)
        t = Text()
        texts.append(first(t) + rng.choice(SEPARATORS) + second(t))
    return list(dict.fromkeys(texts))


def run(argv: tuple[str, ...], text: str) -> list[str]:
    """The lines the text's atoms wrote, one per ``echo`` that ran."""
    with tempfile.TemporaryDirectory() as folder:
        # Pipes for stdout and stderr: reading them to the end waits for
        # background jobs, which hold them until they exit.
        proc = subprocess.Popen(
            [*argv, text],
            cwd=folder,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"},
            start_new_session=True,
        )
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise RuntimeError(f"{argv[0]} did not finish {text!r}") from None
        # Every file: `>>o\r` or `&> o2` sends an atom's line elsewhere.
        lines: list[str] = []
        for name in sorted(os.listdir(folder)):
            with open(
                os.path.join(folder, name), encoding="utf-8", errors="replace", newline=""
            ) as f:
                lines.extend(f.read().split("\n")[:-1])
        # `&> o2` also catches a shell's own complaint about a bad line.
        return [line for line in lines if not _SHELL_ERROR.match(line)]


def run_all(
    shells: dict[str, tuple[str, ...]], texts: list[str]
) -> dict[str, dict[str, list[str]]]:
    """``{text: {shell: lines}}``, run in parallel."""
    jobs = [(text, name, argv) for text in texts for name, argv in shells.items()]
    with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 2) * 2)) as pool:
        results = list(pool.map(lambda job: run(job[2], job[0]), jobs))
    table: dict[str, dict[str, list[str]]] = {text: {} for text in texts}
    for (text, name, _), lines in zip(jobs, results, strict=True):
        table[text][name] = lines
    return table
