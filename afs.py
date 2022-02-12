__all__ = ("ass_font_subset", "FontNotFound")
__version__ = "0.4.0"


from collections import defaultdict
from typing import DefaultDict, Dict, List, Set, Tuple, Iterable, Generator, cast
import os
import re
import logging
from uuid import uuid4
from fontTools.ttLib import TTFont, TTCollection
from fontTools import subset


class FontNotFound(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f'font not found: "{name}"')
        self.name = name


def walk_dir(path: str, recursive: bool) -> Generator[str, None, None]:
    with os.scandir(path) as it:
        for entry in it:
            if entry.is_file():
                yield entry.path
            elif recursive and entry.is_dir():
                yield from walk_dir(entry.path, recursive)


def ass_font_subset(ass_files: Iterable[os.PathLike], fonts_dir: os.PathLike, output_dir: os.PathLike, *, continue_on_font_not_found: bool = False, recursive_fonts_dir: bool = False) -> None:
    # collect fonts
    fonts_dir = os.fsdecode(fonts_dir)
    font_map: DefaultDict[str, Dict[int, TTFont]] = defaultdict(dict)
    fontname_map: Dict[str, str] = {}
    for font_path in walk_dir(fonts_dir, recursive_fonts_dir):
        extname = os.path.splitext(font_path)[1].lower()
        if extname == ".ttc":
            ttc = TTCollection(font_path, recalcBBoxes=False, lazy=True)
            fonts = cast(List[TTFont], ttc.fonts)
        elif extname in (".otf", ".ttf"):
            fonts = cast(List[TTFont], [TTFont(font_path, recalcBBoxes=False, lazy=True)])
        else:
            continue
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
            if fs_selection in font_map[new_fn]:
                logging.warning(f'multiple candidates are found for "{font_names[0]}" fsSelection={fs_selection}; random one is chosen')
            font_map[new_fn][fs_selection] = font
    fn404 = "00000000-0000-0000-0000-000000000000"

    # modify subtitles
    ass_files = [os.fsdecode(p) for p in ass_files]
    char_map: DefaultDict[str, Set[str]] = defaultdict(set)
    fn_reg = re.compile(r"(?<=\\fn)[^\\]+")
    or_reg = re.compile(r"{(.*?)}")
    output_dir = os.fsdecode(output_dir)
    logged_fnf = set()
    if os.listdir(output_dir):
        logging.warning("output directory not empty")
    def sub_in_ranges(pattern: re.Pattern, repl, string: str, spans: Iterable[Tuple[int, int]]):
        last_idx = 0
        buf = []
        for start, end in spans:
            buf.append(string[last_idx:start])
            buf.append(string[start:end])
            last_idx = end
        buf.append(string[last_idx:])
        for i in range(len(buf)):
            if i % 2:
                buf[i] = pattern.sub(repl, buf[i])
        return "".join(buf)
    def or_collect_and_clear(match: re.Match) -> str:
        overrides.append(match)
        return ""
    def fnf(fn: str) -> None:
        exc = FontNotFound(fn)
        if continue_on_font_not_found:
            if fn not in logged_fnf:
                logging.error(exc)
                logged_fnf.add(fn)
        else:
            raise exc from None
    def repl_fn(fn: str, no_at: bool = False, ignore: bool = False) -> str:
        fn_no_at = fn[1:] if fn[0] == "@" else fn
        new_fn = fn404
        try:
            new_fn = fontname_map[fn_no_at]
        except KeyError:
            if not ignore:
                fnf(fn_no_at)
        if fn[0] == "@" and not no_at: new_fn = "@" + new_fn
        return new_fn
    def fn_collect_and_repl(match: re.Match) -> str:
        fn = match.group(0).strip()
        used_fonts.append(repl_fn(fn, True))
        return repl_fn(fn)
    for infn in ass_files:
        outfn = os.path.join(output_dir, os.path.basename(infn))
        with open(infn, "r", encoding="utf-8") as infile, open(outfn, "w", encoding="utf-8-sig", newline="\r\n") as outfile:
            if infile.read(1) != '\ufeff':
                infile.seek(0)
            styles: Dict[str, Dict[str, str]] = {}
            for ln in infile:
                if ln.startswith("Format:"):
                    last_format = [field.strip() for field in ln[7:].split(",")]
                elif ln.startswith("Style:"):
                    style = ln[6:].split(",")
                    style = {k: v.strip() for k, v in zip(last_format, style)}
                    styles[style["Name"]] = style
                    original_fn = style["Fontname"]
                    style["Fontname"] = repl_fn(original_fn, False, True)
                    ln = "Style: " + ",".join(style.values()) + "\n"
                    style["OriginalFontname"] = original_fn
                elif ln.startswith("Dialogue:"):
                    dialog = ln[9:].split(',', len(last_format) - 1)
                    text = dialog[-1].rstrip("\n")
                    dialog.pop()
                    dialog = {k: v.strip() for k, v in zip(last_format, dialog)}
                    style_name = dialog["Style"]
                    style = styles[style_name]
                    overrides: List[re.Match] = []
                    plaintext = or_reg.sub(or_collect_and_clear, text)
                    plaintext = plaintext.replace(r"\h", "\u00A0")
                    plaintext = plaintext.replace(r"\n", "\n")
                    plaintext = plaintext.replace(r"\N", "\n")
                    if plaintext != "":
                        style_font = style["Fontname"]
                        if style_font.startswith("@"):
                            style_font = style_font[1:]
                        if style_font == fn404:
                            original_fn = style["OriginalFontname"]
                            fnf(original_fn[1:] if original_fn.startswith("@") else original_fn)
                        used_fonts = [style_font]
                        text = sub_in_ranges(fn_reg, fn_collect_and_repl, text, [override.span(1) for override in overrides])
                        for fn in used_fonts:
                            char_map[fn].update(plaintext)
                        ln = "Dialogue: " + ",".join(dialog.values()) + "," + text + "\n"
                outfile.write(ln)

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
            name_table.addName(f"Version 0.1;afs.py {__version__}", ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            if "GSUB" in font: trim_g(font["GSUB"])
            if "GPOS" in font: trim_g(font["GPOS"])
            subsetter.subset(font)
            font.save(os.path.join(output_dir, f"{fn}-{fs}.otf"))


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Font subsetter for ASS subtitles.")
    parser.add_argument("ass_files", nargs="+", type=Path, metavar="ASS_FILE", help="the input ASS subtitle file")
    parser.add_argument("--fonts-dir", type=Path, required=True, help="the fonts directory")
    parser.add_argument("--output-dir", type=Path, required=True, help="the output directory (MUST NOT EXISTS)")
    parser.add_argument("--recursive-fonts-dir", action="store_true", help="recursively walk the fonts directory")
    parser.add_argument("--continue-on-font-not-found", action="store_true", help="log and continue when a font is not found, instead of stopping")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    args.output_dir.mkdir()
    ass_font_subset(args.ass_files, args.fonts_dir, args.output_dir, continue_on_font_not_found=args.continue_on_font_not_found, recursive_fonts_dir=args.recursive_fonts_dir)
