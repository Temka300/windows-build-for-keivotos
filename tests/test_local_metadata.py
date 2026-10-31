"""Only supplied tags and descriptions are interpreted; originals retain other fields."""
from pathlib import Path
import json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from services import local_metadata as parser


def test_suwayomi_tags_description_and_ignored_fields():
    value=dict(title='Ignored title',author='Ignored author',description='<b>plain 이야기</b>',genre=['Drama','音楽','Drama'],status='99',unknown={'kept':'original'})
    assert parser.details_json(json.dumps(value).encode())==dict(summary='<b>plain 이야기</b>',tags=['Drama','音楽'])
    assert parser.details_json(b'{"title":"Ignored","author":4,"status":"bad"}')=={}


def test_nfo_tags_description_are_inert_and_other_fields_are_not_interpreted():
    raw=b'<episodedetails><title>Ignored</title><plot>&lt;img src="https://invalid.example"&gt;</plot><director>Ignored</director><genre>Drama</genre><tag>Study</tag><year>2025</year><language>ko</language><watched>true</watched><playcount>8</playcount><thumb>https://invalid.example/art.jpg</thumb></episodedetails>'
    assert parser.nfo(raw)==dict(summary='<img src="https://invalid.example">',tags=['Drama','Study'])


@pytest.mark.parametrize('raw',[b'[]',b'{"description":"a","description":"b"}',b'{"genre":"Drama"}',b'{"description":NaN}',b'{"description":4}',b'{"description":"\\ud800"}',b'{"description":"'+b'a'*16385+b'"}',b' '*(parser.MAX_BYTES+1)],ids=['array','duplicate','genre','nonfinite','description-type','surrogate','long-description','oversize'])
def test_invalid_json_is_not_silently_reinterpreted(raw):
    with pytest.raises(parser.MetadataError):parser.details_json(raw)


@pytest.mark.parametrize('raw',[b'<!DOCTYPE movie [<!ENTITY x "expanded">]><movie><plot>&x;</plot></movie>',b'<!DOCTYPE movie SYSTEM "https://invalid.example/data"><movie/>',b'<movie><plot>',b'<ComicInfo><Title>Not a video standard</Title></ComicInfo>',b'<movie>'+b'<node>'*66+b'</node>'*66+b'</movie>'],ids=['entity','external','malformed','unsupported-root','depth'])
def test_invalid_or_entity_loading_xml_is_refused(raw):
    with pytest.raises(parser.MetadataError):parser.nfo(raw)


def test_explicit_clear_and_inherit_are_different_and_only_two_fields_are_editable():
    assert parser.fields({},personal=True)=={}
    assert parser.fields({'tags':[],'summary':''},personal=True)==dict(tags=[],summary='')
    assert parser.fields({'tags':[],'summary':''})=={}
    for field in ('title','alternative_titles','creators','genres','status','year','language','made_up'):
        with pytest.raises(parser.MetadataError):parser.fields({field:None},personal=True)


def test_comicinfo_supplied_fields_unicode_categories_and_ignored_names():
    raw='<ComicInfo><Series>Ignored</Series><Summary>설명</Summary><Genre>Drama, 이야기</Genre><Tags>Drama, 한글</Tags><CategoriesTachiyomi>Study</CategoriesTachiyomi><Future>preserved</Future></ComicInfo>'.encode()
    assert parser.comic_info(raw)==dict(summary='설명',tags=['Drama','이야기','한글','Study'])
    assert parser.comic_info(b'<ComicInfo><Series>ignored</Series><Writer>ignored</Writer></ComicInfo>')=={}


@pytest.mark.parametrize('raw',[b'<movie/>',b'<ComicInfo><Summary>',b'<!DOCTYPE ComicInfo SYSTEM "https://invalid.example"><ComicInfo/>',b'<ComicInfo><Genre>a</Genre><Genre>b</Genre></ComicInfo>',b'<ComicInfo>'+b'<x>'*66+b'</x>'*66+b'</ComicInfo>',b' '*(parser.MAX_BYTES+1)],ids=['wrong-root','malformed','external','duplicate','deep','oversize'])
def test_comicinfo_rejects_invalid_or_hostile_metadata(raw):
    with pytest.raises(parser.MetadataError):parser.comic_info(raw)
