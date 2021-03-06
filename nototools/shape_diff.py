# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Provides ShapeDiffFinder, which finds differences in OTF/TTF glyph shapes.

ShapeDiffFinder takes in two paths, to font binaries. It then provides methods
which compare these fonts, storing results in a report dictionary. These methods
are `find_area_diffs`, which compares glyph areas, and `find_rendered_diffs`
which compares harfbuzz output using image magick.

Neither comparison is ideal. Glyph areas can be the same even if the shapes are
wildly different. Image comparison is usually either slow (hi-res) or inaccurate
(lo-res). Still, these are usually useful for raising red flags and catching
large errors.
"""


import os
import subprocess
import tempfile

from fontTools.ttLib import TTFont
from glyph_area_pen import GlyphAreaPen

from nototools import hb_input


class ShapeDiffFinder:
    """Provides methods to report diffs in glyph shapes between OT Fonts."""

    def __init__(self, file_a, file_b, stats, ratio_diffs=False):
        self.paths = file_a, file_b
        self.fonts = [TTFont(f) for f in self.paths]
        self.ratio_diffs = ratio_diffs

        stats['compared'] = []
        stats['untested'] = []
        stats['unmatched'] = []
        stats['unicode_mismatch'] = []
        self.stats = stats

        self.basepath = os.path.basename(file_a)

    def find_area_diffs(self):
        """Report differences in glyph areas."""

        self.build_names()

        glyph_sets =[f.getGlyphSet() for f in self.fonts]
        glyph_set_a, glyph_set_b = glyph_sets
        pen_a, pen_b = [GlyphAreaPen(glyph_set) for glyph_set in glyph_sets]

        mismatched = {}
        for name in self.names:
            glyph_set_a[name].draw(pen_a)
            area_a = pen_a.unload()
            glyph_set_b[name].draw(pen_b)
            area_b = pen_b.unload()
            if area_a != area_b:
                mismatched[name] = (area_a, area_b)

        stats = self.stats['compared']
        calc = self._calc_ratio if self.ratio_diffs else self._calc_diff
        for name, areas in mismatched.items():
            stats.append((calc(areas), name, self.basepath, area[0], area[1]))

    def find_rendered_diffs(self, font_size=256, render_path=None):
        """Find diffs of glyphs as rendered by harfbuzz + image magick."""

        self.build_names()
        self.build_reverse_cmap()

        hb_input_generators = [
            hb_input.HbInputGenerator(p, self.reverse_cmap) for p in self.paths]
        path_a, path_b = self.paths
        ordered_names = list(self.names)

        a_png = self._make_tmp_path()
        b_png = self._make_tmp_path()
        cmp_png = self._make_tmp_path()
        diffs_filename = self._make_tmp_path()

        for name in ordered_names:
            hb_args, hb_args_b = [input_generator.input_from_name(name)
                                  for input_generator in hb_input_generators]
            assert hb_args == hb_args_b

            # ignore unreachable characters
            if not hb_args:
                self.stats['untested'].append(name)
                continue

            features, text = hb_args

            # ignore null character
            if text == unichr(0):
                continue

            with open(diffs_filename, 'a') as ofile:
                ofile.write('%s\n' % name)

            subprocess.call([
                'hb-view', '--font-size=%d' % font_size,
                '--output-file=%s' % a_png,
                '--features=%s' % ','.join(features), path_a, text])
            subprocess.call([
                'hb-view', '--font-size=%d' % font_size,
                '--output-file=%s' % b_png,
                '--features=%s' % ','.join(features), path_b, text])

            img_info = subprocess.check_output(['identify', a_png]).split()
            assert img_info[0] == a_png and img_info[1] == 'PNG'
            subprocess.call([
                'convert', '-gravity', 'center', '-background', 'black',
                '-extent', img_info[2], b_png, b_png])

            if render_path:
                output_png = os.path.join(render_path, name + '.png')
                # see for a discussion of this rendering technique:
                # https://github.com/googlei18n/nototools/issues/162#issuecomment-175885431
                subprocess.call([
                    'convert',
                    '(', a_png, '-colorspace', 'gray', ')',
                    '(', b_png, '-colorspace', 'gray', ')',
                    '(', '-clone', '0-1', '-compose', 'darken', '-composite', ')',
                    '-channel', 'RGB', '-combine', output_png])

            with open(diffs_filename, 'a') as ofile:
                subprocess.call(
                    ['compare', '-metric', 'AE', a_png, b_png, cmp_png],
                    stderr=ofile)

        with open(diffs_filename) as ifile:
            lines = ifile.readlines()
        diffs = [(lines[i].strip(), lines[i + 1].strip())
                 for i in range(0, len(lines), 2)]

        os.remove(a_png)
        os.remove(b_png)
        os.remove(cmp_png)
        os.remove(diffs_filename)

        mismatched = {}
        for name, diff in diffs:
            if int(diff) != 0:
                mismatched[name] = int(diff)

        stats = self.stats['compared']
        for name, diff in mismatched.items():
            stats.append((diff, name, self.basepath))

    def build_names(self):
        """Build a list of glyph names shared between the fonts."""

        if hasattr(self, 'names'):
            return

        stats = self.stats['unmatched']
        names_a, names_b = [set(f.getGlyphOrder()) for f in self.fonts]
        if names_a != names_b:
            stats.append((self.basepath, names_a - names_b, names_b - names_a))
        self.names = names_a & names_b

    def build_reverse_cmap(self):
        """Build a map from glyph names to unicode values for the fonts."""

        if hasattr(self, 'reverse_cmap'):
            return

        stats = self.stats['unicode_mismatch']
        reverse_cmaps = [hb_input.build_reverse_cmap(f) for f in self.fonts]
        mismatched = {}
        for name in self.names:
            unival_a, unival_b = [m.get(name) for m in reverse_cmaps]
            if unival_a != unival_b:
                mismatched[name] = (unival_a, unival_b)
        if mismatched:
            stats.append((self.basepath, mismatched.items()))

        # return cmap with only names used consistently between fonts
        self.reverse_cmap = {n: v for n, v in reverse_cmaps[0].items()
                             if n in self.names and n not in mismatched}

    @staticmethod
    def dump(stats, whitelist, out_lines, include_vals, multiple_fonts):
        """Return the results of run diffs.

        Args:
            stats: List of tuples with diff data which is sorted and printed.
            whitelist: Names of glyphs to exclude from report.
            out_lines: Number of diff lines to print.
            include_vals: Include the values which have been diffed in report.
            multiple_fonts: Designates whether stats have been accumulated from
                multiple fonts, if so then font names will be printed as well.
        """

        report = []

        compared = sorted(
            s for s in stats['compared'] if s[1] not in whitelist)
        compared.reverse()
        fmt = '%s %s'
        if include_vals:
            fmt += ' (%s vs %s)'
        if multiple_fonts:
            fmt = '%s ' + fmt
        report.append('%d differences in glyph shape' % len(compared))
        for line in compared[:out_lines]:
            # print <font> <glyph> <vals>; stats are sorted in reverse priority
            line = tuple(reversed(line[:3])) + tuple(line[3:])
            # ignore font name if just one pair of fonts was compared
            if not multiple_fonts:
                line = line[1:]
            report.append(fmt % line)
        report.append('')

        for name in sorted(stats['untested']):
            report.append('not tested (unreachable?): %s' % name)
        report.append('')

        for font, set_a, set_b in stats['unmatched']:
            report.append("Glyph coverage doesn't match in %s" % font)
            report.append('  in A but not B: %s' % sorted(set_a))
            report.append('  in B but not A: %s' % sorted(set_b))
        report.append('')

        for font, mismatches in stats['unicode_mismatch']:
            report.append("Glyph unicode values don't match in %s" % font)
            for name, univals in sorted(mismatches):
                univals = [(('0x%04X' % v) if v else str(v)) for v in univals]
                report.append('  %s: %s in A, %s in B' %
                              (name, univals[0], univals[1]))
        report.append('')

        return '\n'.join(report)

    def _calc_diff(self, vals):
        """Calculate an area difference."""

        a, b = vals
        return abs(a - b)

    def _calc_ratio(self, vals):
        """Calculate an area ratio."""

        a, b = vals
        if not (a or b):
            return 0
        return 1 - min(a, b) / max(a, b)

    def _make_tmp_path(self):
        """Return a temporary path, for use in rendering."""

        handle, path = tempfile.mkstemp()
        os.close(handle)
        return path
