import logging
import sys

from blackduck import Client

from . import global_values


def connect():
    global_values.bd = Client(
        token=global_values.bd_api,
        base_url=global_values.bd_url,
        verify=(not global_values.bd_trustcert),
        timeout=60,
    )
    return global_values.bd


def check_projver(bd, proj, ver):
    params = {
        'q': "name:" + proj,
        'sort': 'name',
    }

    projects = bd.get_resource('projects', params=params)
    for p in projects:
        if p['name'] == proj:
            versions = bd.get_resource('versions', parent=p, params=params)
            for v in versions:
                if v['versionName'] == ver:
                    return v
            break
    else:
        logging.error(f"Project '{proj}' does not exist")
        print('Available projects:')
        for proj_item in bd.get_resource('projects'):
            print(proj_item['name'])
        sys.exit(2)

    logging.error(f"Version '{ver}' does not exist in project '{proj}'")
    sys.exit(2)


def get_project_version(proj, ver):
    bd = connect()
    return check_projver(bd, proj, ver)
