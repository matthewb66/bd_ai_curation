import pytest

from bd_ai_curation.path_filters import is_noise_path


@pytest.mark.parametrize('path', [
    'tmp/cov/java-api-stubs.jar!/com/x/Y.class',
    'idir/tmp/java-api-stubs.jar',
    'opt/app.jar!/foo#tmp/x/java-api-stubs.jar!/bar',
    'Java-API-Stubs.jar',
])
def test_coverity_stub_jar_is_noise(path):
    assert is_noise_path(path)


@pytest.mark.parametrize('path', [
    'idir/emit/c1.h',
    'src/idir/Util.java',
    'lib/java-api-stubs.jar.bak',
    'lib/my-java-api-stubs.jar!/x',
])
def test_idir_folder_alone_is_not_noise(path):
    assert not is_noise_path(path)
