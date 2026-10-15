#!/usr/bin/env python3
"""C/C++ から Fortran の BLAS/LAPACK を呼ぶ箇所に、CHARACTER 引数の隠れた長さ引数を足す。

Fortran は CHARACTER の仮引数ごとに長さを末尾の隠れた引数で受け取る（gfortran 8 以降と flang は size_t）。
SciPy の C は長さを渡さずに呼んでおり、ネイティブでは余った引数が無視されて動くが、規約違反で、wasm では
関数型が一致せずに wasm-ld が呼び出しを trap に差し替える。LAPACK の lapack.h（LAPACK_FORTRAN_STRLEN_END）や
R の FCONE と同じく、宣言には size_t の長さ引数を、呼び出しには長さ（1 文字の引数なら 1）を足す。

各ルーチンの CHARACTER 引数の数は、参照 LAPACK のソースから読む（→ lapack_signatures.py）。

    python3 fortran_strlen.py <LAPACK のソース> <書き換えるファイル>...

宣言（波括弧の外）は `, size_t` を、呼び出しは `, 1` を足す。すでに長さを渡している宣言・呼び出し
（引数の数が「明示の引数 + 長さ」と一致するもの）は触らず、数が合わないものは止める。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lapack_signatures import read_routines  # noqa: E402

# 呼び出しの書き方。BLAS_FUNC(dgemv)・ARNAUD_BLAS(dgemv)・dgemv_ のどれかと、マクロの中で名前を連結する
# BLAS_FUNC(PREFIX ## getrs)・BLAS_FUNC(PREFIX ## L_PREFIX ## trf) の形
NAME = re.compile(r'\b(?:(?:BLAS_FUNC|ARNAUD_BLAS)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*##\s*[A-Za-z0-9_]+)*)\s*\)|([a-z][a-z0-9_]*)_)(?=\s*\()')


def macro_instances(masked, position):
    """position を本体に持つ関数形式マクロの、仮引数の名前と、ファイルの中での展開の実引数の並びを返す。

    @param masked [str] 走査用の写し
    @param position [int] マクロの本体の中の位置
    @return [tuple[list[str], list[list[str]]]] 仮引数の名前と、展開ごとの実引数
    """
    line_start = masked.rfind('\n', 0, position) + 1
    while line_start > 1 and masked[line_start - 2] == '\\':
        line_start = masked.rfind('\n', 0, line_start - 1) + 1
    define = re.match(r'\s*#\s*define\s+(\w+)\(([^)]*)\)', masked[line_start:])
    if not define:
        raise SystemExit(f'{position}: マクロの本体の中の名前の連結を、マクロの定義として読めない')
    macro, params = define.group(1), [p.strip() for p in define.group(2).split(',')]
    instances = []
    for call in re.finditer(r'^\s*' + macro + r'\s*\(([^()]*)\)', masked, re.M):
        instances.append([a.strip() for a in call.group(1).split(',')])
    return params, instances


def lookup(match, masked, routines):
    """呼び出しの名前から、候補になるルーチン（具体的な名前・引数・CHARACTER の引数）を返す。

    マクロの中で名前を連結する形は、そのマクロがファイルの中で展開されるときの実引数を当てはめて、
    展開されるルーチンをすべて候補にする

    @param match [re.Match] NAME の一致
    @param masked [str] 走査用の写し
    @param routines [dict] 名前 → (引数の名前, CHARACTER の引数 → 長さ, 返り値の型)
    @return [list[tuple]] (名前, 引数の名前, CHARACTER の引数 → 長さ) の並び
    """
    expression = match.group(1) or ''
    if '##' not in expression:
        name = expression or match.group(2)
        return [(name, *routines[name][:2])] if name in routines else []
    params, instances = macro_instances(masked, match.start())
    pieces = [piece.strip() for piece in expression.split('##')]
    names = sorted({''.join(args[params.index(p)] if p in params else p for p in pieces) for args in instances})
    return [(name, *routines[name][:2]) for name in names if name in routines]


def mask(text):
    """コメントと文字列・文字のリテラルを同じ長さの空白に置き換えた写しを返す（位置はそのまま）。

    @param text [str] ソース
    @return [str] 走査用の写し
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
        elif text[i] in '"\'':
            quote, j = text[i], i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == '\\' else 1
            j += 1
        else:
            i += 1
            continue
        for k in range(i, min(j, n)):
            if out[k] != '\n':
                out[k] = ' '
        i = j
    return ''.join(out)


def split_args(masked, start):
    """start の `(` に対応する `)` の位置と、引数の区切り（深さ 0 のカンマ）の数を返す。

    @param masked [str] 走査用の写し
    @param start [int] `(` の位置
    @return [tuple[int, list[tuple[int, int]]]] `)` の位置と、各引数の範囲
    """
    depth, args, begin = 0, [], start + 1
    for i in range(start, len(masked)):
        c = masked[i]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                args.append((begin, i))
                # 引数の無い呼び出し `f()` は 0 個。写しの中の文字列は空白なので、中身が空かどうかでは数えない
                return i, [] if args == [(start + 1, i)] and i == start + 1 else args
        elif c == ',' and depth == 1:
            args.append((begin, i))
            begin = i + 1
    raise ValueError('閉じ括弧が無い')


def scope_depth(masked, position):
    """position での波括弧の深さを返す。`extern "C" {` の波括弧は数えない（中の宣言はファイルの外側と同じ扱い）。

    @param masked [str] 走査用の写し（文字列は空白になっているので `extern "C" {` は `extern     {` に見える）
    @param position [int] 位置
    @return [int] 深さ
    """
    stack = []
    for match in re.finditer(r'[{}]', masked[:position]):
        if match.group(0) == '{':
            stack.append(bool(re.search(r'\bextern\s*$', masked[:match.start()])))
        elif stack:
            stack.pop()
    return sum(not linkage for linkage in stack)


def in_define(masked, position):
    """position が #define の行（継続行を含む）の中かを返す。

    @param masked [str] 走査用の写し
    @param position [int] 位置
    @return [bool]
    """
    line_start = masked.rfind('\n', 0, position) + 1
    while line_start > 0 and masked[line_start - 2] == '\\':
        line_start = masked.rfind('\n', 0, line_start - 1) + 1
    return masked[line_start:].lstrip().startswith('#define')


def rewrite(text, routines):
    """ソースの宣言と呼び出しに長さ引数を足し、書き換えたソースと、書き換えた箇所の数を返す。

    @param text [str] ソース
    @param routines [dict] 名前 → (引数の名前, CHARACTER の引数 → 長さ, 返り値の型)
    @return [tuple[str, int]]
    """
    masked = mask(text)
    edits = []
    for match in NAME.finditer(masked):
        candidates = [(name, args, chars) for name, args, chars in lookup(match, masked, routines) if chars]
        if not candidates:
            continue
        paren = masked.index('(', match.end())
        close, spans = split_args(masked, paren)
        line = text.count('\n', 0, match.start()) + 1
        names = ', '.join(name for name, _, _ in candidates)
        # 展開されるルーチンすべてが、長さの要る数（明示の引数）か、渡し済みの数（明示の引数 + 長さ）のどちらかに揃うこと
        if all(len(spans) == len(args) + len(chars) for _, args, chars in candidates):
            continue
        if not all(len(spans) == len(args) for _, args, chars in candidates):
            raise SystemExit(f'{names}（{line} 行目）: 引数が {len(spans)} 個で、ルーチンの引数の数と合わない')
        shapes = {tuple((i, chars[a]) for i, a in enumerate(args) if a in chars) for _, args, chars in candidates}
        if len(shapes) != 1:
            raise SystemExit(f'{names}（{line} 行目）: CHARACTER の引数の位置がルーチンごとに違う')
        name, args, chars = candidates[0]
        lengths = [chars[a] for a in args if a in chars]
        line_start = masked.rfind('\n', 0, match.start()) + 1
        before = masked[line_start:match.start()]
        declaration = (scope_depth(masked, match.start()) == 0 and not in_define(masked, match.start())
                       and not re.search(r'\breturn\b|=', before))
        if declaration:
            # 宣言の引数に名前があれば長さにも名前を付ける（ないなら型だけ）
            named = [re.search(r'(\w+)\s*(?:\[\s*\])?\s*$', text[s:e]) for s, e in spans]
            names = [m.group(1) for m in named] if all(named) and not any(
                text[s:e].strip() in ('char*', 'char *') for s, e in spans) else None
            char_spans = [spans[args.index(a)] for a in args if a in chars]
            extra = ''.join(
                f', size_t {re.search(r"(\w+)\s*$", text[s:e]).group(1)}_len' if names else ', size_t'
                for s, e in char_spans)
        else:
            char_spans = [spans[args.index(a)] for a in args if a in chars]
            extra = ''.join(', 1' if length == '1' else f', strlen({text[s:e].strip()})'
                            for (s, e), length in zip(char_spans, lengths))
        edits.append((close, extra))
    for position, extra in sorted(edits, reverse=True):
        text = text[:position] + extra + text[position:]
    return text, len(edits)


def main():
    """引数のファイルを書き換える。"""
    routines = read_routines(Path(sys.argv[1]))
    for path in map(Path, sys.argv[2:]):
        text = path.read_text()
        rewritten, count = rewrite(text, routines)
        if count:
            path.write_text(rewritten)
            print(f'{path}: {count}')


if __name__ == '__main__':
    main()
