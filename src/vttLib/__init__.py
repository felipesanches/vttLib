from __future__ import print_function, division, absolute_import
import re
import array
from collections import deque

from .parser import AssemblyParser, ParseException

from fontTools.ttLib import newTable, TTLibError
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.misc.py23 import StringIO


class VTTLibError(TTLibError):
    pass


def set_cvt_table(font, data):
    data = re.sub(r"/\*.*?\*/", "", data, flags=re.DOTALL)
    values = array.array("h")
    # control values are defined in VTT Control Program as colon-separated
    # INDEX: VALUE pairs
    for m in re.finditer(r"^\s*([0-9]+):\s*(-?[0-9]+)", data, re.MULTILINE):
        index, value = int(m.group(1)), int(m.group(2))
        for i in range(1 + index - len(values)):
            # missing CV indexes default to zero
            values.append(0)
        values[index] = value
    if len(values):
        if "cvt " not in font:
            font["cvt "] = newTable("cvt ")
        font["cvt "].values = values


def transform_assembly(data):
    tokens = AssemblyParser.parseString(data, parseAll=True)

    push_on = True
    push_indexes = [0]
    stream = [deque()]
    pos = 1
    for t in tokens:
        mnemonic = t.mnemonic

        if mnemonic in ("USEMYMETRICS", "OVERLAP", "OFFSET"):
            # XXX these are not part of the TT instruction set...
            continue

        elif mnemonic == "#PUSHON":
            push_on = True
            continue
        elif mnemonic == "#PUSHOFF":
            push_on = False
            continue

        elif mnemonic == "#BEGIN":
            # XXX shouldn't these be ignored in #PUSHOFF mode?
            push_indexes.append(pos)
            stream.append(deque())
            pos += 1
            continue
        elif mnemonic == "#END":
            pi = push_indexes.pop()
            stack = stream[pi]
            if len(stack):
                stream[pi] = "PUSH[] %s" % " ".join([str(i) for i in stack])
            continue

        elif mnemonic == "#PUSH":
            # XXX push stack items whether or not in #PUSHON/OFF?
            stream.append(
                "PUSH[] %s" % " ".join([str(i) for i in t.stack_items]))
            pos += 1
            continue

        elif mnemonic.startswith(("DLTC", "DLTP", "DELTAP", "DELTAC")):
            assert push_on
            n = len(t.deltas)
            assert n > 0
            stack = stream[push_indexes[-1]]
            stack.appendleft(n)
            for point_index, rel_ppem, step_no in reversed(t.deltas):
                if mnemonic.startswith(("DELTAP", "DELTAC")):
                    rel_ppem -= 9  # subtract the default 'delta base'
                stack.appendleft(point_index)
                # -8: 0, ... -1: 7, 1: 8, ... 8: 15
                selector = (step_no + 7) if step_no > 0 else (step_no + 8)
                stack.appendleft((rel_ppem << 4) | selector)
            if mnemonic.startswith("DLT"):
                mnemonic = mnemonic.replace("DLT", "DELTA")
        else:
            if push_on:
                for i in reversed(t.stack_items):
                    stream[push_indexes[-1]].appendleft(i)
            else:
                assert not t.stack_items

        stream.append("%s[%s]" % (mnemonic, t.flags))
        pos += 1

    assert len(push_indexes) == 1 and push_indexes[0] == 0, push_indexes
    stack = stream[0]
    if len(stack):
        stream[0] = "PUSH[] %s" % " ".join([str(i) for i in stack])

    return "\n".join([i for i in stream if i])


def pformat_tti(program, preserve=False):
    from fontTools.ttLib.tables.ttProgram import _pushCountPat

    assembly = program.getAssembly(preserve=preserve)
    stream = StringIO()
    i = 0
    nInstr = len(assembly)
    while i < nInstr:
        instr = assembly[i]
        stream.write(instr)
        stream.write("\n")
        m = _pushCountPat.match(instr)
        i = i + 1
        if m:
            nValues = int(m.group(1))
            line = []
            j = 0
            for j in range(nValues):
                if j and not (j % 25):
                    stream.write(' '.join(line))
                    stream.write("\n")
                    line = []
                line.append(assembly[i+j])
            stream.write(' '.join(line))
            stream.write("\n")
            i = i + j + 1
    return stream.getvalue()


def make_program(vtt_assembly, name=None):
    try:
        ft_assembly = transform_assembly(vtt_assembly)
    except ParseException as e:
        import sys
        if name:
            sys.stderr.write(
                'An error occurred while parsing "%s" program:\n' % name)
        sys.stderr.write(e.markInputline() + "\n\n")
        raise VTTLibError(e)
    program = Program()
    program.fromAssembly(ft_assembly)
    # need to compile bytecode for PUSH optimization
    program._assemble()
    del program.assembly
    return program


def get_extra_assembly(font, tag):
    if tag not in ("cvt", "cvt ", "prep", "ppgm", "fpgm"):
        raise ValueError("Invalid tag: %r" % tag)
    if tag == "prep":
        tag = "ppgm"
    return _get_assembly(font, tag.strip())


def get_glyph_assembly(font, name):
    return _get_assembly(font, name, is_glyph=True)


def _get_assembly(font, name, is_glyph=False):
    if is_glyph:
        data = font['TSI1'].glyphPrograms[name]
    else:
        data = font['TSI1'].extraPrograms[name]
    # normalize line endings as VTT somehow uses Macintosh-style CR...
    return "\n".join(data.splitlines())


def compile_instructions(font, ship=True):
    if "glyf" not in font:
        raise VTTLibError("Missing 'glyf' table; not a TrueType font")
    if "TSI1" not in font:
        raise VTTLibError("The font contains no 'TSI1' table")

    control_program = get_extra_assembly(font, "cvt")
    set_cvt_table(font, control_program)

    for tag in ("prep", "fpgm"):
        if tag not in font:
            font[tag] = newTable(tag)
        data = get_extra_assembly(font, tag)
        font[tag].program = make_program(data, tag)

    glyf_table = font['glyf']
    for glyph_name in font.getGlyphOrder():
        try:
            data = get_glyph_assembly(font, glyph_name)
        except KeyError:
            continue
        program = make_program(data, glyph_name)
        if program:
            glyph = glyf_table[glyph_name]
            glyph.program = program

    if ship:
        for tag in ("TSI%d" % i for i in (0, 1, 2, 3, 5)):
            if tag in font:
                del font[tag]
