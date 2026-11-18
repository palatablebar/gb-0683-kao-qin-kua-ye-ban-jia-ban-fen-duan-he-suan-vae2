import os
from pathlib import Path
import pytest
from app.workflow import load,run
@pytest.fixture(autouse=True)
def isolated_db(tmp_path,monkeypatch):
    # 每个用例独立数据库，封账与延后账不跨用例泄漏。
    monkeypatch.setenv('DB_PATH',str(tmp_path/'autouse.db'))
@pytest.fixture
def raw():return load('dirty.csv')
@pytest.fixture
def state(raw):return run(raw,{})
@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'test.db'))
    from app.main import create_app
    app=create_app();app.config['TESTING']=True
    return app.test_client()
