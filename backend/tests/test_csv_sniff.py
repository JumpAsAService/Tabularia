"""A CSV uploaded from the editor, which sends the file alone, read as it is: Excel's «CSV»
in Italian is `;`-separated, with decimal commas, in Windows-1252 — it answered 500, or became
text and «Citt�». A column whose type changes after the first 10,000 rows was a 500 too."""
import polars as pl
import pytest

from app.ingest.converters import IngestError, convert_to_parquet
from app.ingest.formats import FileFormat
from app.ingest.models import CsvOptions, IngestOptions
from app.ingest.sniff import sniff

SNIFF = IngestOptions(csv=CsvOptions(sniff=True))


def _read(tmp_path, data: bytes, options=SNIFF) -> pl.DataFrame:
    src, dst = tmp_path / "in.csv", tmp_path / "out.parquet"
    src.write_bytes(data)
    convert_to_parquet(str(src), str(dst), FileFormat.csv, options)
    return pl.read_parquet(dst)


def test_excel_italian_csv(tmp_path):
    data = ("Città ;Prodotto;Prezzo unitario €;Quantità\nModena;Speck;16,125;3\nParma;Brie;1.234,50;10\n"
            "Bologna;Coppa;7,5;1\n").encode("cp1252")
    assert sniff_of(tmp_path, data) == (";", True, "cp1252")
    df = _read(tmp_path, data)
    assert df.columns == ["Città", "Prodotto", "Prezzo unitario €", "Quantità"]
    assert df["Prezzo unitario €"].dtype == pl.Float64 and df["Prezzo unitario €"].to_list()[::2] == [16.125, 7.5]
    assert df["Città"].to_list() == ["Modena", "Parma", "Bologna"]


def sniff_of(tmp_path, data: bytes):
    p = tmp_path / "s.csv"
    p.write_bytes(data)
    s = sniff(str(p))
    return s.separator, s.decimal_comma, s.encoding


@pytest.mark.parametrize("data, expected", [
    (b"a,b,c\n1,2.5,x\n3,4.5,y\n", (",", False, "utf8")),
    (b"a\tb\n1\t2\n", ("\t", False, "utf8")),
    ('a|b\n"x|y"|2\n'.encode(), ("|", False, "utf8")),
    (b"a;b\n1;2.5\n3;4.75\n", (";", False, "utf8")),  # semicolons with dots: no decimal comma
    ("€;b\n1,5;x\n".encode("utf-8"), (";", True, "utf8")),
    (b"solo\n1\n2\n", (",", False, "utf8")),
])
def test_what_is_read_from_the_file(tmp_path, data, expected):
    assert sniff_of(tmp_path, data) == expected


def test_a_type_that_changes_after_the_guessed_rows(tmp_path):
    rows = [f"{i},{i}" for i in range(20_000)] + ["20000,12.5"]
    df = _read(tmp_path, ("id,importo\n" + "\n".join(rows) + "\n").encode())
    assert df["importo"].dtype == pl.Float64 and df["importo"][-1] == 12.5 and df.height == 20_001


def test_a_file_that_is_not_csv_is_refused_saying_so(tmp_path):
    with pytest.raises(IngestError, match="could not be read as CSV"):
        _read(tmp_path, b'a,b\n1,"never closed\n2,3\n' + b"x" * 10, IngestOptions(csv=CsvOptions(sniff=True, has_header=True)))


def test_a_separator_given_is_kept(tmp_path):
    df = _read(tmp_path, b"a;b\n1;2\n", IngestOptions(csv=CsvOptions(separator=",")))
    assert df.columns == ["a;b"]  # as asked: a datasource that says «,» means it


def test_a_thousands_dot_with_semicolons_is_never_one_and_a_half(tmp_path):
    df = _read(tmp_path, "Prodotto;Quantità;Prezzo\nSpeck;1.500;16,50\nBrie;2;7,25\n".encode())
    assert df["Quantità"].to_list() == [1500.0, 2.0] and df["Prezzo"].to_list() == [16.5, 7.25]
    # quantities only, no decimal comma anywhere: still thousands
    df = _read(tmp_path, "Prodotto;Quantità\nSpeck;1.500\nBrie;2.300\n".encode())
    assert df["Quantità"].to_list() == [1500.0, 2300.0]


def test_codes_and_chosen_types_stay_text(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes("Cap;Importo\n00144;10,5\n20121;3,25\n".encode())
    out = tmp_path / "f.parquet"
    convert_to_parquet(str(p), str(out), FileFormat.csv, IngestOptions(csv=SNIFF.csv, dtype_overrides={"Cap": "str"}))
    df = pl.read_parquet(out)
    assert df["Cap"].to_list() == ["00144", "20121"] and df["Importo"].to_list() == [10.5, 3.25]
