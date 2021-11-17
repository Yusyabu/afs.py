from collections import defaultdict
from typing import DefaultDict, Dict, Sequence, Set, List, cast
import os
import re
from uuid import uuid4
from fontTools.ttLib import TTFont, TTCollection
from fontTools import subset
import pysubs2

def ass_font_subset(ass_files: Sequence[os.PathLike], fonts_dir: os.PathLike, output_dir: os.PathLike) -> None:
    # collect chars
    def no_at(fn: str) -> str:
        if fn[0] == "@":
            fn = fn[1:]
        return fn
    ass_files = [os.fsdecode(p) for p in ass_files]
    ass_list = [pysubs2.load(p) for p in ass_files]
    char_map: DefaultDict[str, Set[str]] = defaultdict(set)
    fn_reg = re.compile(r"(?<=\\fn)[^\}\\]+")
    for ass in ass_list:
        for ln in ass.events:
            if ln.is_comment or ln.is_drawing:
                continue
            style = ass.styles[ln.style]
            used_fonts = [no_at(style.fontname)]
            for match in fn_reg.finditer(ln.text):
                used_fonts.append(no_at(match.group(0)))
            plaintext = ln.plaintext
            for fn in used_fonts:
                char_map[fn].update(plaintext)
    
    # filter char_map
    for chars in char_map.values():
        chars.discard("\n")
    
    # collect fonts
    fonts_dir = os.fsdecode(fonts_dir)
    font_files: list[str] = []
    for font_path in os.listdir(fonts_dir):
        if os.path.splitext(font_path)[1].lower() in (".otf", ".ttf", ".ttc"):
            font_files.append(os.path.join(fonts_dir, font_path))
    font_map: DefaultDict[str, Dict[int, TTFont]] = defaultdict(dict)
    fontname_map: Dict[str, str] = {}
    for font_path in font_files:
        print(font_path)
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
    output_dir = os.fsdecode(output_dir)
    def repl_fn(match):
        fn = match if isinstance(match, str) else match.group(0)
        new_fn = fontname_map[no_at(fn)]
        if fn[0] == "@": new_fn = "@" + new_fn
        return new_fn
    for filename, ass in zip(ass_files, ass_list):
        used_styles = set()
        for ln in ass.events:
            if ln.is_comment or ln.is_drawing:
                continue
            ln.text = fn_reg.sub(repl_fn, ln.text)
            used_styles.add(ln.style)
        for style_name in used_styles:
            style = ass.styles[style_name]
            style.fontname = repl_fn(style.fontname)
        ass.save(os.path.join(output_dir, os.path.basename(filename)))
    
    # update char_map
    new_char_map: DefaultDict[str, Set[str]] = defaultdict(set)
    for fn, chars in char_map.items():
        new_fn = fontname_map[fn]
        new_char_map[new_fn].update(chars)
    char_map = new_char_map
    
    # subset fonts
    font_style_map = {
        0b0000000: "Regular",
        0b0000001: "Italic",
        0b0100000: "Bold",
        0b0100001: "Bold Italic",
        0b1000000: "Regular",
    }
    for fn, chars in char_map.items():
        subsetter = subset.Subsetter(subset.Options(hinting=False))
        subsetter.populate(text="".join(chars))
        for fs, font in font_map[fn].items():
            name_table = font["name"]
            name_table.names = []
            name_table.addName(fn, ((3, 1, 1033),), 0)
            name_table.addName(font_style_map.get(fs & ~(3 << 7), "Unknown"), ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            name_table.addName("Version 0.1;afspy 0.1", ((3, 1, 1033),), 0)
            name_table.addName(fn, ((3, 1, 1033),), 0)
            subsetter.subset(font)
            font.save(os.path.join(output_dir, f"{fn}-{fs}.otf"))
