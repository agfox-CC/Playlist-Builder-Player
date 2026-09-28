from housekit.textnorm import norm_artist, norm_title, song_key, split_version


def test_apostrophe_variants_collapse():
    assert norm_title("Don't Stop Believin'") == norm_title("Dont Stop Believing") == norm_title("Don’t Stop Believin")


def test_and_ampersand_and_articles():
    assert norm_title("The Devil & The Deep Blue Sea") == norm_title("Devil and the Deep Blue Sea")
    assert norm_artist("The Rolling Stones") == norm_artist("Rolling Stones")


def test_feat_and_remaster_stripped():
    assert norm_title("Uptown Funk (feat. Bruno Mars)") == norm_title("Uptown Funk")
    assert norm_title("Free Fallin' - 2011 Remaster") == norm_title("Free Fallin")
    assert norm_title("Mr. Brightside (Radio Edit)") == norm_title("Mr Brightside")
    assert norm_title("Jessie's Girl [Live]") == norm_title("Jessies Girl")


def test_split_version_keeps_real_parentheticals():
    title, ver = split_version("Escape (The Pina Colada Song)")
    assert title == "Escape (The Pina Colada Song)"
    assert ver == ""
    title, ver = split_version("Hotel California (Live) (Remastered 2013)")
    assert title == "Hotel California"
    assert "Live" in ver and "Remastered" in ver


def test_artist_credits_stripped():
    assert norm_artist("Tom Petty and the Heartbreakers") == norm_artist("Tom Petty")
    assert norm_artist("Bruno Mars feat. Cardi B") == "bruno mars"


def test_song_key_stable():
    assert song_key("Journey", "Don't Stop Believin'") == song_key("journey", "Dont Stop Believing (Live)")


def test_artist_spelling_variants_collapse():
    assert norm_artist("Doobie Brothers, The") == norm_artist("The Doobie Brothers") == "doobie brothers"
    assert norm_artist("England Dan, John Ford Coley") == norm_artist("England Dan & John Ford Coley") == "england dan john ford coley"
    assert norm_artist("Earth, Wind & Fire") == norm_artist("Earth Wind and Fire") == "earth wind fire"
    assert norm_artist("Bob Seger & The Silver Bullet Band") == "bob seger"
    assert norm_artist("Tyler, The Creator") == "tyler the creator"
