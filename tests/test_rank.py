import datetime as dt
import textwrap

import pytest

from daily_brief import brief, db, rank

BRIEF = textwrap.dedent("""\
    ---
    version: 1
    generated: 2026-09-27
    source: hand
    tv_safe: true
    priorities:
      - id: p1
        text: "Land an AI product manager role"
        signals: [AI PM interviews, evals]
      - id: p2
        text: "Reliable agents"
        signals: [agent reliability, memory]
    active_projects: ["ExampleProject"]
    already_known: ["RAG"]
    ignore: ["crypto"]
    depth: {default: practitioner}
    tone: "Calm."
    ---
    """)


@pytest.fixture
def setup(tmp_path):
    p = tmp_path / "brief.md"
    p.write_text(BRIEF)
    b = brief.load_brief(p, [])
    conn = db.connect(tmp_path / "b.db")
    conn.execute("insert into sources(id,segment,kind,url,name,backfill) values (1,'B','rss','https://f','L',1)")
    return b, conn


def add(conn, guid, title, days_ago, backcat=1, status="discovered"):
    pub = (dt.datetime(2026, 9, 26, tzinfo=dt.UTC) - dt.timedelta(days=days_ago)).isoformat()
    return conn.execute("insert into items(source_id,guid,title,published_at,is_backcatalog,status) "
                        "values (1,?,?,?,?,?)", (guid, title, pub, backcat, status)).lastrowid


def test_terms_drop_stopwords_and_short_words():
    assert rank.terms("How to run your AI product team like a lab") == {"ai", "product", "team", "lab", "run"}


def test_ranks_relevant_titles_first(setup):
    b, conn = setup
    add(conn, "a", "Enterprise sales alpha", 10)
    add(conn, "b", "How AI product managers should run evals", 400)
    add(conn, "c", "Building agent memory that stays reliable", 200)
    picks = rank.pick_backcatalog(conn, b, n=3)
    guids = [conn.execute("select guid from items where id=?", (i,)).fetchone()[0] for i in picks]
    assert set(guids[:2]) == {"b", "c"} and guids[2] == "a"  # both relevant titles beat the off-brief one


def test_ignore_terms_push_down(setup):
    b, conn = setup
    add(conn, "a", "AI product lessons from crypto", 5)
    add(conn, "b", "AI product lessons", 500)
    assert conn.execute("select guid from items where id=?", (rank.pick_backcatalog(conn, b, n=1)[0],)).fetchone()[0] == "b"


def test_skips_new_items_and_already_processed(setup):
    b, conn = setup
    add(conn, "new", "AI product evals", 1, backcat=0)
    add(conn, "done", "AI product evals again", 30, status="extracted")
    add(conn, "dead", "AI product evals forever", 30, status="failed")
    keep = add(conn, "ok", "Some other AI story", 30)
    assert rank.pick_backcatalog(conn, b, n=3) == [keep]


def test_tie_breaks_on_recency(setup):
    b, conn = setup
    old = add(conn, "old", "Agent memory", 900)
    new = add(conn, "new", "Agent memory", 90)
    assert rank.pick_backcatalog(conn, b, n=2) == [new, old]


def test_specific_match_beats_many_generic_matches(setup):
    b, conn = setup
    # the generic words appear in most titles (low IDF); "evals" is rare, so one specific hit should win
    for k in range(8):
        add(conn, f"g{k}", f"Land the AI product manager role, story {k}", 50 + k)
    add(conn, "generic", "Land the product role: AI product manager career story", 20)
    specific = add(conn, "specific", "Evals are the new unit tests", 300)
    assert rank.pick_backcatalog(conn, b, n=1) == [specific]


def test_backcatalog_is_per_segment(setup):
    b, conn = setup
    conn.execute("insert into sources(id,segment,kind,url,name,backfill) values (2,'A','youtube','https://y','Example Channel',0)")
    lenny = add(conn, "l", "Agent memory evals", 10)
    assert rank.pick_backcatalog(conn, b, 5, segment="B") == [lenny]
    assert rank.pick_backcatalog(conn, b, 5, segment="A") == []


def test_title_score_counts_signals_and_penalises_ignored_terms(setup):
    b, _ = setup
    wanted, avoid = rank.brief_stems(b)
    assert rank.title_score("Evals for AI PM interviews", wanted, avoid) > rank.title_score("A cooking show", wanted, avoid)
    assert rank.title_score("Crypto evals", wanted, avoid) < rank.title_score("Evals", wanted, avoid)
