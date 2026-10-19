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
            versions = list(bd.get_resource('versions', parent=p, params=params))
            for v in versions:
                if v['versionName'] == ver:
                    return v
            break
    else:
        logging.error(f"Project '{proj}' does not exist")
        matches = sorted(p_item['name'] for p_item in bd.get_resource('projects')
                         if proj.lower() in p_item['name'].lower())
        if matches:
            print(f"Projects containing '{proj}':")
            for name in matches:
                print(name)
        else:
            print(f"No projects contain '{proj}'")
        sys.exit(2)

    logging.error(f"Version '{ver}' does not exist in project '{proj}'")
    print(f"Available versions in project '{proj}':")
    for v in versions:
        print(v['versionName'])
    sys.exit(2)


def get_project_version(proj, ver):
    bd = connect()
    return check_projver(bd, proj, ver)
