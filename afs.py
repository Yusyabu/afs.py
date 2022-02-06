from collections import defaultdict
from collections.abc import MutableMapping
from typing import DefaultDict, Dict, Set, Iterable, cast
import os
import re
import logging
from uuid import uuid4
from fontTools.ttLib import TTFont, TTCollection
from fontTools import subset
import pysubs2


class CaseInsensitiveDict(MutableMapping):
    def __init__(self, *args, **kwargs):
        self._s = {k.lower(): (k, v) for k, v in dict(*args, **kwargs).items()}

    def __getitem__(self, k):
        return self._s[k.lower()][1]

    def __setitem__(self, k, v):
        self._s[k.lower()] = (k, v)

    def __delitem__(self, k):
        del self._s[k.lower()]

    def __iter__(self):
        return iter(v[0] for v in self._s.values())

    def __len__(self):
        return len(self._s)


class FontNotFound(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f'font not found: "{name}"')
        self.name = name


def ass_font_subset(ass_files: Iterable[os.PathLike], fonts_dir: os.PathLike, output_dir: os.PathLike, *, continue_on_font_not_found: bool = False) -> None:
    # collect fonts
    fonts_dir = os.fsdecode(fonts_dir)
    font_files: list[str] = []
    for font_path in os.listdir(fonts_dir):
        if os.path.splitext(font_path)[1].lower() in (".otf", ".ttf", ".ttc"):
            font_files.append(os.path.join(fonts_dir, font_path))
    font_map: DefaultDict[str, Dict[int, TTFont]] = defaultdict(dict)
    fontname_map = CaseInsensitiveDict()
    for font_path in font_files:
        if not os.path.isfile(font_path): continue
        if os.path.splitext(font_path)[1].lower() == ".ttc":
            ttc = TTCollection(font_path, recalcBBoxes=False, lazy=True)
            fonts = cast(list[TTFont], ttc.fonts)
        else:
            fonts = [TTFont(font_path, recalcBBoxes=False, lazy=True)]
        for font in fonts:
            name_table = font["name"]
            font_names = []
            for record in name_table.names:
                if record.platformID == 3 and record.nameID == 1:
                    # this is vsfilter's lookup behavior
                    font_names.append(record.toUnicode())
            fs_selection = font["OS/2"].fsSelection
            for fn in font_names:
                try:
                    new_fn = fontname_map[fn]
                except KeyError:
                    pass
                else:
                    break
            else:
                new_fn = str(uuid4())
            for fn in font_names:
                fontname_map[fn] = new_fn
            font_map[new_fn][fs_selection] = font

    # modify subtitles
    ass_files = [os.fsdecode(p) for p in ass_files]
    ass_list = [pysubs2.load(p, format_="ass") for p in ass_files]
    char_map: DefaultDict[str, Set[str]] = defaultdict(set)
    fn_reg = re.compile(r"(?<=\\fn)[^\}\\]+")
    output_dir = os.fsdecode(output_dir)
    logged_fnf = set()
    if os.listdir(output_dir):
        logging.warning("output directory not empty")
    def repl_fn(fn: str, no_at: bool = False) -> str:
        fn_no_at = fn[1:] if fn[0] == "@" else fn
        new_fn = "Arial"
        try:
            new_fn = fontname_map[fn_no_at]
        except KeyError:
            exc = FontNotFound(fn_no_at)
            if continue_on_font_not_found:
                if fn_no_at not in logged_fnf:
                    logging.error(exc)
                    logged_fnf.add(fn_no_at)
            else:
                raise exc from None
        if fn[0] == "@" and not no_at: new_fn = "@" + new_fn
        return new_fn
    def fn_collect_and_repl(match: re.Match) -> str:
        fn = match.group(0).strip()
        used_fonts.append(repl_fn(fn, True))
        return repl_fn(fn)
    for filename, ass in zip(ass_files, ass_list):
        used_styles = set()
        for ln in ass.events:
            if ln.is_comment or ln.is_drawing or ln.plaintext == "":
                continue
            plaintext = ln.plaintext
            style = ass.styles[ln.style]
            used_fonts = [repl_fn(style.fontname, True)]
            ln.text = fn_reg.sub(fn_collect_and_repl, ln.text)
            for fn in used_fonts:
                char_map[fn].update(plaintext)
            used_styles.add(ln.style)
        for style_name in used_styles:
            style = ass.styles[style_name]
            style.fontname = repl_fn(style.fontname)
        ass.save(os.path.join(output_dir, os.path.basename(filename)))
    for chars in char_map.values():
        chars.discard("\n")

    # subset fonts
    def trim_g(g):
        for script in g.table.ScriptList.ScriptRecord:
            if script.ScriptTag == "DFLT":
                preserve_script = script
                break
        else:
            # XXX: I don't know why but there exist fonts without a DFLT script like 方正准圆
            preserve_script = g.table.ScriptList.ScriptRecord[0]
        g.subset_script_tags([preserve_script.ScriptTag])
    font_style_map = { 0b0000000: "Regular", 0b0000001: "Italic", 0b0100000: "Bold", 0b0100001: "Bold Italic", 0b1000000: "Regular" }
    for fn, chars in char_map.items():
        subsetter = subset.Subsetter(subset.Options(hinting=False, layout_features=["vert", "vrt2"]))
        subsetter.populate(text="".join(chars))
        for fs, font in font_map[fn].items():
            name_table = font["name"]
            name_table.names = []
            name_table.addName(fn, ((3, 1, 1033),), 0)
            name_table.addName(font_style_map.get(fs & ~(3 << 7), "Unknown"), ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            name_table.addName("Version 0.1;afs.py 0.2", ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            if "GSUB" in font: trim_g(font["GSUB"])
            if "GPOS" in font: trim_g(font["GPOS"])
            subsetter.subset(font)
            font.save(os.path.join(output_dir, f"{fn}-{fs}.otf"))


__all__ = ("ass_font_subset", "FontNotFound")
__version__ = "0.3.0"


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Font subsetter for ASS subtitles.")
    parser.add_argument("ass_files", nargs="+", type=Path, metavar="ASS_FILE", help="the input ASS subtitle file")
    parser.add_argument("--fonts-dir", type=Path, required=True, help="the fonts directory")
    parser.add_argument("--output-dir", type=Path, required=True, help="the output directory (MUST NOT EXISTS)")
    parser.add_argument("--delete-output-dir", action="store_true", help="DELETE output directory before processing, if it exists")
    parser.add_argument("--continue-on-font-not-found", action="store_true", help="log and continue when a font is not found, instead of stopping")
    parser.add_argument("--fonttools-verbose", action="store_true", help="show verbose fontTools logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.delete_output_dir:
        import shutil
        try:
            shutil.rmtree(args.output_dir)
        except FileNotFoundError:
            pass
    args.output_dir.mkdir()

    if not args.fonttools_verbose:
        logging.getLogger("fontTools").setLevel(logging.ERROR)

    ass_font_subset(args.ass_files, args.fonts_dir, args.output_dir, continue_on_font_not_found=args.continue_on_font_not_found)
