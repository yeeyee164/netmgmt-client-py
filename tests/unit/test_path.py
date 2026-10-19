import pytest
#from specs.gnmi.gnmi_pb2 import Path, PathElem
from specs.gnmi.gnmi_pb2 import Path, PathElem
from modules.path import (
    parse_path,
    parse_xpath_keys,
    to_path_elem,
    MalformedXPathError,
    MalformedXPathKeyError,
    EmptyPathElemNameError
)

# This test checks the overall string-typed path structures
@pytest.mark.parametrize("name, str_path, gnmi_path, ok, error", [
    # successful cases
    ("empty path","", Path(), True, None),
    ("slash","/", Path(), True, None),
    ("named root path without slash","e", Path(elem=[
        PathElem(name="e")
    ]), True, None),
    ("named root path","/e", Path(elem=[
        PathElem(name="e")
    ]), True, None),
    ("path with two elements","/e1/e2", Path(elem=[
        PathElem(name="e1"),
        PathElem(name="e2")
    ]), True, None),
    ("path with two elements with key","/e1/e2[k=v]", Path(elem=[
        PathElem(name="e1"),
        PathElem(name="e2", key={'k':'v'})
    ]), True, None),
    ("path with two elements with two keys","/e1/e2[k1=v1][k2=v2]", Path(elem=[
        PathElem(name="e1"),
        PathElem(name="e2", key={'k1':'v1', 'k2':'v2'})
    ]), True, None),
    ("path with origin only","origin:", Path(origin='origin'), True, None),
    ("path with origin and slash","origin:/", Path(origin='origin'), True, None),
    ("path with origin and root path","origin:/root", Path(
        origin='origin',
        elem=[PathElem(name='root')] 
    ), True, None),
    ("path with empty stuffs",":/", Path(), True, None),
    ("path with origin and two elements","origin:/e1/e2", Path(
        origin='origin',
        elem=[
            PathElem(name='e1'),
            PathElem(name='e2')
        ]
    ), True, None),
    ("path with origin and two elements with key","origin:/e1/e2[k=v]", Path(
        origin='origin',
        elem=[
            PathElem(name='e1'),
            PathElem(name='e2', key={'k':'v'})
        ]
    ), True, None),
    ("path with origin and two elements with two keys","origin:/e1/e2[k1=v1][k2=v2]", Path(
        origin='origin',
        elem=[
            PathElem(name='e1'),
            PathElem(name='e2', key={'k1':'v1', 'k2':'v2'})
        ]
    ), True, None),
    ("path containing backslashes",r"origin:/e1\[/e2\][k1=v1]", Path(
        origin='origin',
        elem=[
            PathElem(name='e1\\['),
            PathElem(name='e2\\]', key={'k1':'v1'}),
        ]
    ), True, None),
    ("path with meaninful keys",r"origin:/e1/e2[address=1.2.3.4/24]/e3[name=\[hello_dev\]]", Path(
        origin='origin',
        elem=[
            PathElem(name='e1'),
            PathElem(name='e2', key={'address':'1.2.3.4/24'}),
            PathElem(name='e3', key={'name':'[hello_dev]'}),
        ]
    ), True, None),
    ("path with key contains space, colon","origin:/e1/e2[k=d a :ta:]", Path(
        origin='origin',
        elem=[
            PathElem(name='e1'),
            PathElem(name='e2', key={'k':'d a :ta:'}),
        ]
    ), True, None),
    # absolutely failed cases
    ("path with unfinished bracket","/e1/e2[k=data", Path(), False, MalformedXPathError),
    ("path without opening bracket","/e1/e2k=data]", Path(), False, MalformedXPathError),
    ("path with key without equal character ","/e1/e2[k]", Path(), False, MalformedXPathKeyError),
])
def test_parse_path(name: str, str_path:str, gnmi_path: Path, ok: bool, error):
    try:
        test_path = parse_path(str_path)

        assert test_path.__eq__(gnmi_path) == ok
    except (MalformedXPathError, MalformedXPathKeyError, EmptyPathElemNameError) as e1:
        assert e1.__class__ == error

# This test checks the formats of key notation in path
@pytest.mark.parametrize("name, kv, exp", [
    # successful cases
    ("no key", "", {'out':{}, 'err':None}),
    ("one_key", "[k=v]", {'out':{'k':'v'}, 'err':None}),
    ("two key", "[k1=v1][k2=v2]", {
        'out':{'k1':'v1', 'k2':'v2'},
        'err':None
    }),
    ("unterminated key", "[k=v", {'out':None, 'err':MalformedXPathKeyError}),
    ("has no value", "[k=]", {'out':None, 'err':MalformedXPathKeyError}),
    ("has no key", "[=v]", {'out':None, 'err':MalformedXPathKeyError}),
    ("contains bracket notation", "[k=[v]", {'out':None, 'err':MalformedXPathKeyError}),
    ("contains bracket notation with backslash in value", r"[k=\[\]v]", {
        'out':{'k':'[]v'},
        'err':None
    }),
    ("contains bracket notation with backslash in key", r"[\[\]k=v]", {
        'out':{'[]k':'v'},
        'err':None
    }),
    ("seemingly 'nested' key", r"[\[k=\]v]", {
        'out':{'[k':']v'},
        'err':None
    })
])
def test_parse_xpath_keys(name: str, kv: str, exp: dict):
    try:
        keyval = parse_xpath_keys(kv)

        assert keyval.__eq__(exp['out']) == True
    except MalformedXPathKeyError as e:
        assert e.__class__ == exp['err']


# This test checks the generated PathElem with respect of elem[key=value] string
@pytest.mark.parametrize("name, kv, exp", [
    ("elem only", "elem1", {
        'out': PathElem(name='elem1'),
        'err': None
    })
])
def test_path_elem(name: str, kv: str, exp: dict):
    try:
        path_elem = to_path_elem(kv)

        assert path_elem.__eq__(exp['out'])
    except MalformedXPathError as e:
        assert e.__class__ == exp['err']