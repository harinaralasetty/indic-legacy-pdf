# Notice and attribution

This repository originated as a fork of **legacylipi** by Biplob Biswas
(<https://github.com/biswasbiplob/legacylipi>), licensed under MIT as declared in its
`pyproject.toml`; upstream ships no separate LICENSE file. Credit for the inherited
code remains with the original author.

Additions in this repository by Hari Naralasetty:

- Telugu "glyph-ID" PDF text recovery by reverse shaping (`legacylipi recover-text`,
  automatic detection as `glyph-id`, integration into translate/convert/extract and the API)
- Related tests, documentation and optional `glyph` dependency extra
- Priyaanka Telugu legacy-font research in `research/telugu-legacy-priyaanka/`

Test fixtures licensed under the SIL Open Font License 1.1:

- `tests/fixtures/NotoSansTelugu-Regular.ttf` (see `tests/fixtures/OFL-NotoSansTelugu.txt`)
- `tests/fixtures/NotoSerifTelugu-Regular.ttf` (see `tests/fixtures/OFL-NotoSerifTelugu.txt`)

The MIT license text applies to the combined work:

MIT License - Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify, merge,
publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to
whom the Software is furnished to do so, subject to the following conditions: The above
attribution and this permission notice shall be included in all copies or substantial portions
of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
