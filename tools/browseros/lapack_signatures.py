"""参照 BLAS/LAPACK の Fortran ソースから、各ルーチンの引数と CHARACTER 引数の長さを読む。

CHARACTER の仮引数は、呼び出し側が隠れた長さ引数を末尾に渡す（→ fortran_strlen.py）。その数と、長さが
固定（`CHARACTER` / `CHARACTER*1`）か可変（`CHARACTER*(*)`）かをここで読む。
"""
import re
from pathlib import Path

# 参照 LAPACK の SRC/Makefile が INSTALL/ から取るもの（ALLAUX・SCLAUX・DZLAUX）
INSTALL_USED = {'ilaver', 'lsame', 'slamch', 'dlamch', 'sroundup_lwork', 'droundup_lwork'}
TYPES = {'DOUBLE PRECISION': 'double', 'REAL': 'float', 'INTEGER': 'int', 'LOGICAL': 'int'}
HEAD = re.compile(r'^(?:RECURSIVE\s+|PURE\s+|ELEMENTAL\s+)*(?:(.*?)\s+)?(SUBROUTINE|FUNCTION)\s+(\w+)\s*\((.*?)\)', re.I)
CHARACTER = re.compile(
    r'^CHARACTER\s*(\*\s*\(\s*\*\s*\)|\*\s*\d+|\(\s*(?:len\s*=\s*)?([*\d]+)\s*\))?\s*(?:,[^:]*)?(?:::)?\s*(.*)$', re.I)


def logical_lines(text, free):
    """継続行をつなぎ、コメントを落とした文の並びを返す。

    @param text [str] Fortran のソース
    @param free [bool] 自由形式（.f90）か
    @return [list[str]] 文
    """
    lines = []
    for raw in text.split('\n'):
        if free:
            line = raw.split('!')[0].rstrip()
            if not line.strip():
                continue
            if lines and lines[-1].endswith('&'):
                lines[-1] = lines[-1][:-1] + ' ' + line.strip().lstrip('&')
            else:
                lines.append(line.strip())
            continue
        if not raw.strip() or raw[0] in 'Cc*!' or raw.lstrip().startswith(('!', '#')):
            continue
        body = raw[:72].split('!')[0]
        if len(body) > 5 and body[5] not in ' 0' and lines:
            lines[-1] += ' ' + body[6:].strip()
        else:
            lines.append(body[6:].strip() if len(body) > 6 else body.strip())
    return lines


def split_top(text):
    """括弧の外のカンマで区切る。

    @param text [str] 宣言の並び
    @return [list[str]]
    """
    depth, current, parts = 0, '', []
    for c in text:
        depth += c == '('
        depth -= c == ')'
        if c == ',' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += c
    return parts + [current]


def parse(path):
    """1 ファイルの最初のルーチンを読む。

    @param path [Path] ソース
    @return [tuple | None] (名前, 引数の名前, CHARACTER の引数 → 長さ（'1' か '*'）, 返り値の型)
    """
    lines = logical_lines(path.read_text(errors='replace'), path.suffix.lower() == '.f90')
    for i, line in enumerate(lines):
        head = HEAD.match(line)
        if not head:
            continue
        prefix, kind, name, arguments = head.groups()
        args = [a.strip().lower() for a in arguments.split(',') if a.strip()]
        body = []
        for statement in lines[i + 1:]:
            if re.match(r'^END(\s+(SUBROUTINE|FUNCTION)\b.*)?\s*$', statement, re.I):
                break
            body.append(statement)
        chars = {}
        for statement in body:
            declared = CHARACTER.match(statement)
            if not declared:
                continue
            spec = (declared.group(1) or '').replace(' ', '')
            length = '*' if '*)' in spec or declared.group(2) == '*' else (re.sub(r'\D', '', spec) or '1')
            for item in split_top(declared.group(3)):
                variable = re.sub(r'\(.*', '', item).split('*')[0].strip().lower()
                if variable:
                    chars[variable] = length
        result = None
        if kind.upper() == 'FUNCTION':
            p = (prefix or '').upper().strip()
            if p in TYPES:
                result = TYPES[p]
            elif p.startswith('CHARACTER'):
                result = 'char'
            elif p.startswith(('COMPLEX', 'DOUBLE COMPLEX')):
                result = 'complex'
            else:
                result = 'other'
        return name.lower(), args, {a: chars[a] for a in args if a in chars}, result
    return None


def read_routines(root):
    """BLAS/SRC・SRC・INSTALL のルーチンを読む。

    @param root [Path] LAPACK のソースの根
    @return [dict] 名前 → (引数の名前, CHARACTER の引数 → 長さ, 返り値の型)
    """
    routines = {}
    for directory in ('BLAS/SRC', 'SRC', 'INSTALL'):
        for path in sorted((Path(root) / directory).iterdir()):
            if path.suffix.lower() not in ('.f', '.f90'):
                continue
            if directory == 'INSTALL' and path.stem not in INSTALL_USED:
                continue
            routine = parse(path)
            if routine:
                routines.setdefault(routine[0], routine[1:])
    return routines
