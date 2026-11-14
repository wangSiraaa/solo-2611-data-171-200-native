"""Helpers that synthesize small in-memory PDFs for the test suite.

The shipped fixtures cover the happy path; these builders construct sources
that exercise the preflight refusal paths (encryption, JavaScript, GoToR,
forms, ...) and destination edge cases (direct /Dest, rotated/cropped pages,
omitted named destinations).
"""

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def _blank_page(width=400, height=500, rotation=0, crop=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    if rotation:
        page[NameObject("/Rotate")] = NumberObject(rotation)
    if crop is not None:
        page[NameObject("/CropBox")] = ArrayObject(
            NumberObject(v) for v in crop)
    return writer, page


def make_simple_pdf(path, page_count=3, page_size=(400, 500)):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=page_size[0], height=page_size[1])
    with open(path, "wb") as handle:
        writer.write(handle)


def make_link_pdf(path, *, link_kind="explicit", target_page_index=1,
                  link_page_index=0, action="/GoTo", named_map=None,
                  rotation=0, crop=None):
    """A PDF whose chosen page carries one /Link annotation.

    link_kind: "explicit" writes an explicit /Dest array; "named" writes a
    GoTo action whose /D is a name string and adds a /Names/Dests entry.
    action:   "/GoTo", "/GoToR", "/URI" or "/JavaScript".
    """
    writer = PdfWriter()
    pages = []
    for _ in range(3):
        page = writer.add_blank_page(width=400, height=500)
        if rotation:
            page[NameObject("/Rotate")] = NumberObject(rotation)
        if crop is not None:
            page[NameObject("/CropBox")] = ArrayObject(
                NumberObject(v) for v in crop)
        pages.append(page)

    target_ref = pages[target_page_index].indirect_reference
    if link_kind == "explicit":
        destination = ArrayObject([target_ref, NameObject("/Fit")])
    else:
        destination = TextStringObject("details")

    if action == "/GoTo":
        action_dict = DictionaryObject({
            NameObject("/S"): NameObject("/GoTo"),
            NameObject("/D"): destination,
        })
        action_ref = writer._add_object(action_dict)
        link_holder = {NameObject("/A"): action_ref}
    elif action == "/GoToR":
        action_dict = DictionaryObject({
            NameObject("/S"): NameObject("/GoToR"),
            NameObject("/F"): DictionaryObject(
                {NameObject("/F"): TextStringObject("other.pdf")}),
            NameObject("/D"): TextStringObject("remote-dest"),
        })
        link_holder = {NameObject("/A"): writer._add_object(action_dict)}
    elif action == "/URI":
        action_dict = DictionaryObject({
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("https://example.test"),
        })
        link_holder = {NameObject("/A"): writer._add_object(action_dict)}
    elif action == "/JavaScript":
        action_dict = DictionaryObject({
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("app.alert('x')"),
        })
        link_holder = {NameObject("/A"): writer._add_object(action_dict)}
    else:
        raise ValueError(action)

    annotation = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): ArrayObject(
            NumberObject(v) for v in (35, 375, 220, 400)),
        **link_holder,
    })
    annot_ref = writer._add_object(annotation)
    link_page = pages[link_page_index]
    link_page[NameObject("/Annots")] = ArrayObject([annot_ref])

    if link_kind == "named" and named_map is None:
        dest_array = ArrayObject([target_ref, NameObject("/Fit")])
        dests = DictionaryObject({
            NameObject("/Names"): ArrayObject(
                [TextStringObject("details"), dest_array]),
        })
        names = DictionaryObject({
            NameObject("/Dests"): writer._add_object(dests),
        })
        writer._root_object[NameObject("/Names")] = writer._add_object(names)
    elif named_map is not None:
        pairs = ArrayObject()
        for name, target_index in named_map.items():
            pairs.append(TextStringObject(name))
            pairs.append(ArrayObject(
                [pages[target_index].indirect_reference, NameObject("/Fit")]))
        dests = DictionaryObject({NameObject("/Names"): pairs})
        names = DictionaryObject({
            NameObject("/Dests"): writer._add_object(dests)})
        writer._root_object[NameObject("/Names")] = writer._add_object(names)

    with open(path, "wb") as handle:
        writer.write(handle)


def make_direct_dest_link_pdf(path, target_page_index=1):
    """A link using the /Dest key directly (no action dictionary)."""
    writer = PdfWriter()
    pages = [writer.add_blank_page(width=400, height=500) for _ in range(3)]
    annotation = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): ArrayObject(
            NumberObject(v) for v in (10, 10, 100, 50)),
        NameObject("/Dest"): ArrayObject(
            [pages[target_page_index].indirect_reference, NameObject("/Fit")]),
    })
    annot_ref = writer._add_object(annotation)
    pages[0][NameObject("/Annots")] = ArrayObject([annot_ref])
    with open(path, "wb") as handle:
        writer.write(handle)


def make_form_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=500)
    acro = DictionaryObject({NameObject("/Fields"): ArrayObject()})
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(acro)
    with open(path, "wb") as handle:
        writer.write(handle)


def make_encrypted_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=500)
    # RC4 encryption is supported by pypdf's built-in fallback crypto and is
    # enough to exercise the encrypted-PDF refusal path.
    writer.encrypt(user_password="secret", owner_password="owner",
                   algorithm="RC4-128")
    with open(path, "wb") as handle:
        writer.write(handle)


def make_document_javascript_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=500)
    js_action = DictionaryObject({
        NameObject("/S"): NameObject("/JavaScript"),
        NameObject("/JS"): TextStringObject("this.print();"),
    })
    names = DictionaryObject({
        NameObject("/JavaScript"): ArrayObject([
            TextStringObject("onLoad"), writer._add_object(js_action)]),
    })
    writer._root_object[NameObject("/Names")] = writer._add_object(names)
    with open(path, "wb") as handle:
        writer.write(handle)
