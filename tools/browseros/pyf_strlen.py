#!/usr/bin/env python3
"""f2py の .pyf.src の callstatement / callprotoargument に、CHARACTER 引数の隠れた長さ引数を足す。

f2py は callstatement の C をそのまま使うので、Fortran の BLAS/LAPACK を長さなしで呼ぶ書き方がそのまま
生成物に残る（→ fortran_strlen.py と同じ規約違反）。callstatement の `(*f2py_func)(...)` には長さ（1 文字の
引数なら 1）を、callprotoargument には size_t を足す。

各ブロックが呼ぶルーチンは、scipy 自身のテンプレート展開器（tools/generate_f2pymod.py の expand_sub）で
ブロックを展開して求める。展開に要る `<prefix2=s,d>` などの定義は、ブロックより前の本文と、そのファイルを
include する親の .pyf.src から集める（generate_f2pymod の process_str と同じ）。展開したルーチンのすべてが
callstatement の引数の数と合い、CHARACTER の位置が揃うときだけ書き換え、合わなければ止める。

    python3 pyf_strlen.py <LAPACK のソース> <scipy のソース> <書き換える .pyf.src>...
"""
import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fortran_strlen import mask, split_args  # noqa: E402
from lapack_signatures import read_routines, split_top  # noqa: E402

# 名前の中にもテンプレートが入る（`<prefix6><sy,\0,\0,\0, he,\4>r`。<...> の中には空白もある）
HEADER = re.compile(r'^[ \t]*(?:[\w<>*()]+[ \t]+)*(?:subroutine|function)[ \t]+((?:[\w=,\\]|<[^>]*>)+)[ \t]*\(', re.I | re.M)
# 展開すると `end subroutinespttrs` のように空白なしで名前が続く
END = re.compile(r'^[ \t]*end[ \t]+(?:subroutine|function)', re.I | re.M)


def load_generator(scipy_root):
    """scipy の tools/generate_f2pymod.py を読み込む。

    @param scipy_root [Path] scipy のソースの根
    @return [module]
    """
    spec = importlib.util.spec_from_file_location('generate_f2pymod', scipy_root / 'tools' / 'generate_f2pymod.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def concrete_routines(path, generator):
    """path の各ルーチンのブロックが展開される Fortran の名前を、ブロックの先頭の位置ごとに返す。

    generate_f2pymod の process_str と同じく、path を include する親の .pyf.src を include ごと展開した本文を
    前から処理し、ブロックの前の本文の定義と、前のブロックの中の定義（`<prefix6=...>`）を積み上げながら
    各ブロックを展開する。path の本文は展開した本文の中にそのまま入っているので、その範囲のブロックを
    path の中の位置に読み替える

    @param path [Path] 書き換える .pyf.src
    @param generator [module] generate_f2pymod
    @return [dict[int, list[str]]] path の中のブロックの先頭（subroutine の行）→ fortranname（無ければ名前）の並び
    """
    text = path.read_text()
    parents = sorted(parent for parent in path.parent.glob('*.pyf.src')
                     if path.name in re.findall(r"^\s*include\s+'([^']+)'", parent.read_text(), re.M))
    if not parents:
        raise SystemExit(f'{path.name}: include する .pyf.src が無い')
    # LP64 と ILP64（*_64.pyf.src）の両方の親から include されるものは、どちらで展開しても同じ名前になることを確かめる
    results = [concrete_routines_in(path, text, parent, generator) for parent in parents]
    for other, parent in zip(results[1:], parents[1:]):
        if other != results[0]:
            raise SystemExit(f'{path.name}: {parents[0].name} と {parent.name} で展開した名前が違う')
    return results[0]


def concrete_routines_in(path, text, parent, generator):
    """親の .pyf.src 1 つについて、concrete_routines と同じものを返す。

    @param path [Path] 書き換える .pyf.src
    @param text [str] path の本文
    @param parent [Path] path を include する .pyf.src
    @param generator [module] generate_f2pymod
    @return [dict[int, list[str]]]
    """
    resolved = ''.join(generator.resolve_includes(str(parent)))
    start = resolved.find(text)
    if start < 0 or resolved.find(text, start + 1) >= 0:
        raise SystemExit(f'{path.name}: {parent.name} を展開した本文の中に、本文がちょうど 1 か所に見つからない')
    names = dict(generator._special_names)
    concrete = {}
    previous_end = 0
    for block_start, block_end in generator.parse_structure(resolved):
        names.update(generator.find_and_remove_repl_patterns(resolved[previous_end:block_start])[1])
        previous_end = block_end
        expanded = generator.expand_sub(resolved[block_start:block_end], names)
        if not start <= block_start < start + len(text):
            continue
        header = HEADER.search(resolved, block_start, block_end)
        routines = []
        for each in HEADER.finditer(expanded):
            end = END.search(expanded, each.end())
            body = expanded[each.end():end.start() if end else len(expanded)]
            # fortranname は `F_FUNC(dlamch,DLAMCH)` のように名前の変換のマクロで書くこともある
            fortranname = re.search(r'^\s*fortranname\s+(?:F_FUNC\(\s*)?(\w+)', body, re.M)
            routines.append((fortranname.group(1) if fortranname else each.group(1)).lower())
        concrete[header.start() - start] = routines
    return concrete


def rewrite(path, routines, generator):
    """1 つの .pyf.src を書き換え、書き換えた本文と箇所の数を返す。

    @param path [Path] .pyf.src
    @param routines [dict] 名前 → (引数の名前, CHARACTER の引数 → 長さ, 返り値の型)
    @param generator [module] generate_f2pymod
    @return [tuple[str, int]]
    """
    text = path.read_text()
    concrete_of = concrete_routines(path, generator)
    edits = []
    for header in HEADER.finditer(text):
        end = END.search(text, header.end())
        block = text[header.end():end.start() if end else len(text)]
        if not re.search(r'^\s*callstatement\b', block, re.M):
            continue
        line = text.count('\n', 0, header.start()) + 1
        if header.start() not in concrete_of:
            raise SystemExit(f'{path.name}（{line} 行目）: generate_f2pymod の展開でこのブロックが見つからない')
        concrete = concrete_of[header.start()]
        found = [(name, *routines[name][:2]) for name in concrete if name in routines]
        if not any(chars for _, _, chars in found):
            continue
        if len(found) != len(concrete):
            raise SystemExit(f'{path.name}（{line} 行目）: {", ".join(concrete)} の一部が LAPACK に無い')
        # C として読むのは callstatement の文（& で続く行を含む）の中だけ。ブロックの Fortran のコメント（`!` 以降）には
        # アポストロフィが入ることがあり、ブロックごと C として読むと文字リテラルの始まりと取り違える
        statement = re.search(r'^[ \t]*callstatement\b((?:[^\n]*&[ \t]*\n)*[^\n]*)', block, re.M)
        offset = statement.start(1)
        code = block[offset:statement.end(1)]
        masked_code = mask(code)
        lengths_of_call = None
        for call in re.finditer(r'\(\*f2py_func\)\s*\(', masked_code):
            close, spans = split_args(masked_code, call.end() - 1)
            close += offset
            spans = [(s + offset, e + offset) for s, e in spans]
            if all(len(args) + len(chars) == len(spans) for _, args, chars in found):
                continue
            if not all(len(args) == len(spans) for _, args, _ in found):
                raise SystemExit(f'{path.name}（{line} 行目）: callstatement の引数が {len(spans)} 個で、'
                                 f'{", ".join(concrete)} の引数の数と合わない')
            shapes = {tuple((i, chars[a]) for i, a in enumerate(args) if a in chars) for _, args, chars in found}
            if len(shapes) != 1:
                raise SystemExit(f'{path.name}（{line} 行目）: CHARACTER の引数の位置が {", ".join(concrete)} で違う')
            _, args, chars = found[0]
            char_args = [a for a in args if a in chars]
            extra = ''.join(', 1' if chars[a] == '1' else f', strlen({block[s:e].strip()})'
                            for a, (s, e) in zip(char_args, [spans[args.index(a)] for a in char_args]))
            edits.append((header.end() + close, extra))
            lengths_of_call = len(chars)
        if lengths_of_call is None:
            continue
        # 行末の & で次の行に続く書き方がある
        proto = re.search(r'^(\s*callprotoargument\s+)((?:[^\n]*&[ \t]*\n)*[^\n]*?)[ \t]*$', block, re.M)
        if not proto:
            raise SystemExit(f'{path.name}（{line} 行目）: callstatement に callprotoargument が無い')
        # `<F_INT,F_INT,float,double>*` や関数ポインタの型 `F_INT(*)(a*,b*)` の中のカンマは区切りではない
        types = [t for t in split_top(re.sub(r'<[^>]*>', 'T', proto.group(2)).replace('&', ' ')) if t.strip()]
        if not all(len(types) == len(args) for _, args, _ in found):
            raise SystemExit(f'{path.name}（{line} 行目）: callprotoargument の型が {len(types)} 個で合わない')
        edits.append((header.end() + proto.end(2), ',size_t' * lengths_of_call))
    for position, extra in sorted(edits, reverse=True):
        text = text[:position] + extra + text[position:]
    return text, len(edits)


def main():
    """引数の .pyf.src を書き換える。"""
    routines = read_routines(Path(sys.argv[1]))
    generator = load_generator(Path(sys.argv[2]))
    for path in map(Path, sys.argv[3:]):
        rewritten, count = rewrite(path, routines, generator)
        if count:
            path.write_text(rewritten)
            print(f'{path}: {count}')


if __name__ == '__main__':
    main()
