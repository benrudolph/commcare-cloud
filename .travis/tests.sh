#!/bin/bash

if [[ ${TEST} = 'main' ]]
then

    test_syntax() {
        ansible-playbook -i ansible/inventories/test ansible/deploy_stack.yml --syntax-check
    }

    test_localsettings() {
        ansible-playbook -i ansible/inventories/test ansible/deploy_stack.yml -e '@ansible/vars/dev/dev_private.yml' -e '@ansible/vars/dev/dev_public.yml' --tags=commcarehq
        sudo python -m py_compile /home/cchq/www/dev/current/localsettings.py
    }

    test_help_cache() {
        diff <(ansible -h) commcare-cloud/commcare_cloud/help_cache/ansible.txt
        diff <(ansible-playbook -h) commcare-cloud/commcare_cloud/help_cache/ansible-playbook.txt
    }

    test_syntax
    test_localsettings
    test_help_cache
    nosetests

elif [[ ${TEST} = 'prove-deploy' ]]
then
    fail() {
        [[ 'a' = 'b' ]]
    }
fi
