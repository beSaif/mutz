"""Honest no match (gap 5) and not covered (gap 6) helpers: deterministic, no model.

UNINDEXED lists sources we do NOT crawl that might hold the answer, with the topics that point to them.
Suggestions are only hints for the officer; nothing here is searched.
"""
import csv
from pathlib import Path

from search.text import tokenize

SOURCE_NAMES = {
    "artorg": "ARTORG Center for Biomedical Engineering Research (UniBE)",
    "dcb": "Diabetes Center Berne",
}

UNINDEXED = [
    {"id": "insel", "name": "Inselspital, Bern University Hospital", "url": "https://www.insel.ch/",
     "topics": "clinic clinical hospital patient patients surgery surgeon trial trials cardiology oncology tumour "
               "tumor orthopaedics orthopedics radiology imaging intensive care Klinik Spital Patienten Chirurgie "
               "Studie hôpital chirurgie essai clinique"},
    {"id": "bfh", "name": "Bern University of Applied Sciences (BFH), incl. the Energy Storage Research Centre in Biel",
     "url": "https://www.bfh.ch/",
     "topics": "battery batteries cell cells chemistry lithium energy storage electrochemistry recycling engineering "
               "manufacturing production automotive mechatronics materials wood food robotics microtechnology "
               "precision machining medtech device prototype Batterie Batterien Energiespeicher Akku Fertigung "
               "Produktion Werkstoffe batterie stockage énergie fabrication"},
    {"id": "sitem", "name": "sitem-insel, Swiss Institute for Translational and Entrepreneurial Medicine",
     "url": "https://www.sitem-insel.ch/",
     "topics": "translational medtech regulatory approval MDR startup start-up implant implants device clinical "
               "translation Zulassung Medizintechnik"},
    {"id": "zefix", "name": "Zefix, the Swiss commercial register (companies, suppliers; roadmap)",
     "url": "https://www.zefix.admin.ch/",
     "topics": "company companies supplier suppliers manufacturer manufacturers vendor contract manufacturing OEM "
               "distributor Firma Firmen Unternehmen Zulieferer Lieferant Hersteller fournisseur fabricant entreprise"},
]

# Extra topic words for unibe sites in the organizers' inventory that we did not crawl.
_UNIBE_TOPICS = {
    "physik.unibe.ch": "physics laser detector space particle battery materials Physik physique",
    "phinst.unibe.ch": "physics detector space mass spectrometry particle Physik",
    "inf.unibe.ch": "computer science software artificial intelligence machine learning AI algorithm data Informatik",
    "vetsuisse.unibe.ch": "veterinary animal animals livestock Tier Tiere vétérinaire",
    "biology.unibe.ch": "biology ecology plant evolution Biologie",
    "ips.unibe.ch": "plant plants agriculture crop Pflanzen",
    "geo.unibe.ch": "geology mining minerals rock lithium raw materials Geologie",
    "oeschger.unibe.ch": "climate climate change carbon Klima",
    "cde.unibe.ch": "sustainability development environment Nachhaltigkeit",
    "psy.unibe.ch": "psychology behaviour behavior mental Psychologie",
    "math.unibe.ch": "mathematics statistics Mathematik",
    "csh.unibe.ch": "space planets habitability astrobiology",
    "ikim.unibe.ch": "complementary integrative medicine",
    "irm.unibe.ch": "forensic legal medicine toxicology",
}


def _unibe_unindexed():
    f = Path("inventory/unibe_all.tsv")
    out = []
    if not f.exists():
        return out
    with open(f) as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if row.get("in_seeds") == "yes" or row["subdomain"] in ("artorg.unibe.ch", "dcberne.com"):
                continue
            topics = _UNIBE_TOPICS.get(row["subdomain"])
            if topics:
                out.append({"id": "unibe_" + row["subdomain"].split(".")[0], "name": row["description"],
                            "url": row["url"], "topics": topics})
    return out


def suggest_unindexed(terms_text: str, limit=3):
    """Unindexed sources whose topic words overlap the query and its expansions. Always returns
    Inselspital and BFH as defaults when nothing overlaps, since they are the largest gaps."""
    q = set(tokenize(terms_text))
    scored = []
    for s in UNINDEXED + _unibe_unindexed():
        hits = q & set(tokenize(s["topics"]))
        if hits:
            scored.append((len(hits), s))
    scored.sort(key=lambda x: -x[0])
    out = [{"id": s["id"], "name": s["name"], "url": s["url"]} for _, s in scored[:limit]]
    if not out:
        out = [{"id": s["id"], "name": s["name"], "url": s["url"]} for s in UNINDEXED[:2]]
    return out


SUPPLIER_WORDS = set(tokenize(
    "supplier suppliers manufacturer manufacturers vendor vendors distributor distributors OEM "
    "contract manufacturer contract manufacturing subcontractor subcontractors "
    "Zulieferer Lieferant Lieferanten Hersteller Auftragsfertiger Auftragsfertigung "
    "fournisseur fournisseurs fabricant fabricants sous-traitant"))


def is_supplier_need(text: str) -> bool:
    return bool(set(tokenize(text)) & SUPPLIER_WORDS)


NOT_COVERED_MSG = ("Companies and suppliers are not covered yet: the indexed sources are academic (UniBE, ARTORG, "
                   "Diabetes Center Berne). Next source on the roadmap: Zefix, the public commercial register, and "
                   "cantonal cluster lists.")
