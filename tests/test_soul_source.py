import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from cryptography.fernet import Fernet
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from soul_source import SourceError, load_service_settings, load_snapshot


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'music_library.db'
    con = sqlite3.connect(path)
    con.executescript('''
      CREATE TABLE profiles(id INTEGER PRIMARY KEY,name TEXT,navidrome_username TEXT,navidrome_password TEXT);
      CREATE TABLE mirrored_playlists(id INTEGER PRIMARY KEY,profile_id INTEGER,name TEXT,source TEXT,custom_name TEXT);
      CREATE TABLE mirrored_playlist_tracks(id INTEGER PRIMARY KEY,playlist_id INTEGER,position INTEGER,track_name TEXT,
        artist_name TEXT,album_name TEXT,duration_ms INTEGER,source_track_id TEXT,extra_data TEXT);
      CREATE TABLE tracks(id TEXT PRIMARY KEY,title TEXT,artist_id TEXT,album_id TEXT,file_path TEXT,server_source TEXT,
        track_artist TEXT,duration INTEGER,spotify_track_id TEXT,deezer_id TEXT,isrc TEXT);
      CREATE TABLE artists(id TEXT PRIMARY KEY,name TEXT);
      CREATE TABLE albums(id TEXT PRIMARY KEY,title TEXT);
      CREATE TABLE track_downloads(id INTEGER PRIMARY KEY,file_path TEXT,status TEXT,track_title TEXT,track_artist TEXT,
        track_album TEXT,spotify_track_id TEXT,deezer_track_id TEXT,isrc TEXT);
      CREATE TABLE manual_library_track_matches(id INTEGER PRIMARY KEY,profile_id INTEGER,source TEXT,source_track_id TEXT,
        library_track_id TEXT);
      CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
      INSERT INTO profiles VALUES(1,'alice','alice',' test secret '),(2,'bob','bob','dummy');
      INSERT INTO mirrored_playlists VALUES(10,1,'Spotify source','spotify',NULL),(20,2,'Bob mix','spotify',NULL);
      INSERT INTO artists VALUES('ar','Artist');
      INSERT INTO albums VALUES('al','Album'),('al2','Different album');
      INSERT INTO mirrored_playlist_tracks VALUES(1,10,0,'Song','Artist','Album',200000,'sp1','{}');
      INSERT INTO tracks VALUES('nd1','Song','ar','al','/music/Bob/song.flac','navidrome','Artist',200000,'sp1',NULL,NULL);
    ''')
    con.commit()
    con.close()
    return path


def change(db, sql, values=()):
    with sqlite3.connect(db) as con:
        con.execute(sql, values)


def track(db):
    return load_snapshot(db, ['alice'])['profiles'][0]['playlists'][0]['tracks'][0]


def test_read_only_snapshot_keeps_password_bytes_and_exact_other_root_source(db):
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    snapshot = load_snapshot(db, ['alice'])
    profile = snapshot['profiles'][0]
    assert profile['name'] == 'alice'
    assert profile['navidrome_password'] == ' test secret '
    assert [p['id'] for p in profile['playlists']] == [10]
    assert profile['playlists'][0]['tracks'][0]['source_paths'] == ['/music/Bob/song.flac']
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert list(db.parent.iterdir()) == [db]


def test_completed_transfer_path_without_navidrome_scan(db):
    change(db, 'DELETE FROM tracks')
    change(db, 'INSERT INTO track_downloads VALUES(1,?,?,?,?,?,?,?,?)',
           ('/app/Transfer/Artist/Album/song.flac','completed','Song','Artist','Album','sp1',None,None))
    change(db, 'INSERT INTO track_downloads VALUES(2,?,?,?,?,?,?,?,?)',
           ('/app/Transfer/failed.flac','failed','Song','Artist','Album','sp1',None,None))
    assert track(db)['source_paths'] == ['/app/Transfer/Artist/Album/song.flac']


def test_encrypted_personal_credentials_existing_key_only(db):
    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b' dummy secret ').decode()
    change(db, 'UPDATE profiles SET navidrome_password=? WHERE id=1', (token,))
    with pytest.raises(SourceError, match='key was not found'):
        load_snapshot(db, ['alice'])
    (db.parent / '.encryption_key').write_bytes(key)
    assert load_snapshot(db, ['alice'])['profiles'][0]['navidrome_password'] == ' dummy secret '


def test_wrong_key_fails_without_leaking_token(db):
    token = Fernet(Fernet.generate_key()).encrypt(b'never print me').decode()
    change(db, 'UPDATE profiles SET navidrome_password=? WHERE id=1', (token,))
    (db.parent / '.encryption_key').write_bytes(Fernet.generate_key())
    with pytest.raises(SourceError) as error:
        load_snapshot(db, ['alice'])
    assert token not in str(error.value)
    assert 'never print me' not in str(error.value)


def test_required_schema_and_selection_fail_explicitly(db):
    with pytest.raises(SourceError, match='different profile'):
        load_snapshot(db, ['alice'], {20})
    with pytest.raises(SourceError, match='missing or ambiguous'):
        load_snapshot(db, ['unknown'])
    change(db, 'DROP TABLE track_downloads')
    with pytest.raises(SourceError, match='missing track_downloads'):
        load_snapshot(db, ['alice'])


def test_no_service_credential_fallback(db):
    change(db, 'UPDATE profiles SET navidrome_password=NULL WHERE id=1')
    with pytest.raises(SourceError, match='no personal'):
        load_snapshot(db, ['alice'])


def test_wrong_metadata_and_wrong_duration_do_not_reuse_external_id(db):
    change(db, "UPDATE tracks SET title='Wrong song'")
    assert track(db)['source_paths'] == []
    change(db, "UPDATE tracks SET title='Song', duration=260000")
    assert track(db)['source_paths'] == []


def test_ambiguous_album_without_exact_id_is_not_guessed(db):
    change(db, 'UPDATE tracks SET spotify_track_id=NULL')
    change(db, "INSERT INTO tracks VALUES('nd2','Song','ar','al2','/music/Shared/other.flac','navidrome','Artist',200000,NULL,NULL,NULL)")
    assert track(db)['source_paths'] == ['/music/Bob/song.flac']
    change(db, "UPDATE mirrored_playlist_tracks SET album_name=''")
    assert track(db)['source_paths'] == []


def test_discovery_exclusion_and_later_manual_repair(db):
    change(db, 'UPDATE mirrored_playlist_tracks SET extra_data=?', (json.dumps({'unmatched_by_user':True}),))
    blocked_tracks = load_snapshot(db, ['alice'])['profiles'][0]['playlists'][0]['tracks']
    assert len(blocked_tracks) == 1
    assert blocked_tracks[0]['blocked'] is True
    assert blocked_tracks[0]['source_paths'] == []
    assert blocked_tracks[0]['reason'] == 'manual_metadata_repair_required'
    extra = {'unmatched_by_user':True,'discovered':True,'manual_match':True,
             'matched_data':{'name':'Song','artists':[{'name':'Artist'}],'album':{'name':'Album'},'duration_ms':200000}}
    change(db, 'UPDATE mirrored_playlist_tracks SET track_name=?,extra_data=?', ('Misspelled',json.dumps(extra)))
    assert not track(db).get('blocked')
    assert track(db)['title'] == 'Song'
    assert track(db)['source_paths'] == ['/music/Bob/song.flac']


def test_manual_match_is_profile_specific_and_cached_id_requires_album(db):
    change(db, "UPDATE tracks SET spotify_track_id=NULL, album_id='al2'")
    change(db, "INSERT INTO manual_library_track_matches VALUES(1,2,'spotify','sp1','nd1')")
    change(db, 'UPDATE mirrored_playlist_tracks SET extra_data=?', (json.dumps({'library_track_id':'nd1'}),))
    assert track(db)['source_paths'] == []
    change(db, 'UPDATE manual_library_track_matches SET profile_id=1')
    assert track(db)['source_paths'] == ['/music/Bob/song.flac']


def test_order_duplicates_and_display_name_preserved(db):
    change(db, "INSERT INTO mirrored_playlist_tracks VALUES(2,10,1,'Song','Artist','Album',200000,'sp1','{}')")
    change(db, "UPDATE mirrored_playlists SET custom_name='My mix' WHERE id=10")
    playlist = load_snapshot(db, ['alice'], {10})['profiles'][0]['playlists'][0]
    assert playlist['name'] == 'My mix'
    assert len(playlist['tracks']) == 2
    assert playlist['tracks'][0]['key'] == playlist['tracks'][1]['key']


def test_bad_extra_data_is_not_silently_empty(db):
    change(db, "UPDATE mirrored_playlist_tracks SET extra_data='broken'")
    with pytest.raises(SourceError, match='invalid metadata'):
        load_snapshot(db, ['alice'])


def test_service_config_db_priority_and_decryption(db, tmp_path):
    key = Fernet.generate_key()
    (tmp_path / '.encryption_key').write_bytes(key)
    config = {'navidrome': {'base_url':'http://navidrome.example:4533','username':'soulsync',
              'password':Fernet(key).encrypt(b'service secret').decode()},
              'active_media_server':'navidrome','soulseek':{'transfer_path':'/app/Transfer'}}
    change(db, 'INSERT INTO metadata VALUES(?,?)', ('app_config',json.dumps(config)))
    file = tmp_path / 'config.json'
    file.write_text('{"navidrome":{"base_url":"https://wrong.invalid"}}')
    settings = load_service_settings(db, file)
    assert settings == {'url':'http://navidrome.example:4533','username':'soulsync','password':'service secret',
                        'active_media_server':'navidrome','transfer_path':'/app/Transfer'}


def test_service_file_fallback_uses_legacy_key_path_without_moving(db, tmp_path):
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    key = Fernet.generate_key()
    key_file = config_dir / '.encryption_key'
    key_file.write_bytes(key)
    file = config_dir / 'config.json'
    file.write_text(json.dumps({'navidrome':{'password':Fernet(key).encrypt(b'legacy').decode()}}))
    assert load_service_settings(db, file)['password'] == 'legacy'
    assert key_file.read_bytes() == key
    assert not (db.parent / '.encryption_key').exists()


def test_corrupt_db_config_does_not_fall_back_to_potentially_wrong_server(db, tmp_path):
    change(db, 'INSERT INTO metadata VALUES(?,?)', ('app_config','broken'))
    file = tmp_path / 'config.json'
    file.write_text('{"navidrome":{"base_url":"https://wrong.invalid"}}')
    with pytest.raises(SourceError, match='could not be read safely'):
        load_service_settings(db, file)
