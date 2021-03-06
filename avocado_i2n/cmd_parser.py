import sys
import os
import re

from avocado.core.output import LOG_JOB as log
from avocado.core.settings import settings

from . import params_parser as param


def params_from_cmd(args):
    """
    Take care of command line overwriting, parameter preparation,
    setup and cleanup chains, and paths/utilities for all host controls.

    :param args: command line arguments
    :type args: :py:class:`argparse.Namespace`
    """
    root_path = settings.get_value('i2n.common', 'suite_path', default=None)
    sys.path.insert(1, os.path.join(root_path, "utils"))

    # validate typed vm names and possible vm specific restrictions
    args.available_vms = available_vms = param.all_vms()
    args.selected_vms = selected_vms = list(available_vms)
    # If the command line restrictions don't contain any of our primary restrictions
    # (all|normal|gui|nongui|minimal|none), we add "only <default>" to the list where <default> is the
    # primary restriction definition found in the configs. If the configs are also
    # not defining any default, we ultimately add "only all". You can have any futher
    # restrictions like "only=curl" only in the command line.
    primary_tests_restrictions = ["all", "normal", "gui", "nongui", "minimal", "none"]
    use_tests_default = True
    with_nontrivial_restrictions = False
    use_vms_default = {vm_name: True for vm_name in available_vms}
    # the tests string includes the test restrictions while the vm strings include the ones for the vm variants
    tests_str = ""
    vm_strs = {}
    # the run string includes only pure parameters
    param_str = ""
    for cmd_param in args.params:
        try:
            (key, value) = re.findall("(\w+)=(.*)", cmd_param)[0]
            if key == "only" or key == "no":
                # detect if this is the primary restriction to escape defaults
                if value in primary_tests_restrictions:
                    use_tests_default = False
                # detect if this is a second (i.e. additional) restriction
                if tests_str != "":
                    with_nontrivial_restrictions = True
                # main test restriction part
                tests_str += "%s %s\n" % (key, value)
            elif key.startswith("only_") or key.startswith("no_"):
                for vm_name in available_vms:
                    if re.match("(only|no)_%s" % vm_name, key):
                        # escape defaults for this vm and use the command line
                        use_vms_default[vm_name] = False
                        # detect new restricted vm
                        if vm_name not in vm_strs:
                            vm_strs[vm_name] = ""
                        # main vm restriction part
                        vm_strs[vm_name] += "%s %s\n" % (key.replace("_%s" % vm_name, ""), value)
            # NOTE: comma in a parameter sense implies the same as space in config file
            elif key == "vms":
                # NOTE: no restrictions of the required vms are allowed during tests since
                # these are specified by each test (allowed only for manual setup steps)
                selected_vms[:] = value.split(",")
                for vm_name in selected_vms:
                    if vm_name not in available_vms:
                        raise ValueError("The vm '%s' is not among the supported vms: "
                                         "%s" % (vm_name, ", ".join(available_vms)))
            else:
                # NOTE: comma on the command line is space in a config file
                value = value.replace(",", " ")
                param_str += "%s = %s\n" % (key, value)
        except IndexError:
            pass
    args.param_str = param_str
    log.debug("Parsed param string '%s'", param_str)

    # get minimal configurations and parse defaults if no command line arguments
    tests_params = param.prepare_params(base_file="groups-base.cfg",
                                        ovrwrt_file=param.tests_ovrwrt_file,
                                        ovrwrt_str=param_str)
    tests_str += param_str
    if use_tests_default:
        default = tests_params.get("default_only", "all")
        if default not in primary_tests_restrictions:
            raise ValueError("Invalid primary restriction 'only=%s'! It has to be one "
                             "of %s" % (default, ", ".join(primary_tests_restrictions)))
        tests_str += "only %s\n" % default
    args.tests_str = tests_str
    log.debug("Parsed tests string '%s'", tests_str)

    vms_params = param.prepare_params(base_file="guest-base.cfg",
                                      ovrwrt_dict={"vms": " ".join(selected_vms)},
                                      ovrwrt_file=param.vms_ovrwrt_file,
                                      ovrwrt_str=param_str)
    for vm_name in available_vms:
        # some selected vms might not be restricted on the command line so check again
        if vm_name not in vm_strs:
            vm_strs[vm_name] = ""
        vm_strs[vm_name] += param_str
        if use_vms_default[vm_name]:
            default = vms_params.get("default_only_%s" % vm_name)
            if default is None:
                raise ValueError("No default variant restriction found for %s!" % vm_name)
            vm_strs[vm_name] += "only %s\n" % default
    args.vm_strs = vm_strs
    log.debug("Parsed vm strings '%s'", vm_strs)

    # control against invoking internal tests
    control_parser = param.prepare_parser(base_file="sets.cfg",
                                          ovrwrt_file=param.tests_ovrwrt_file,
                                          ovrwrt_str=tests_str)
    if with_nontrivial_restrictions:
        for d in control_parser.get_dicts():
            if ".internal." in d["name"] or ".original." in d["name"]:
                # the user should have gotten empty Cartesian product by now but check just in case
                raise ValueError("You cannot restrict to internal tests from the command line.\n"
                                 "Please use the provided manual steps or automated setup policies "
                                 "to run an internal test %s." % d["name"])

    # prefix for all tests of the current run making it possible to perform multiple runs in one command
    args.prefix = ""

    # log into files for each major level the way it was done for autotest
    args.store_logging_stream = [":10", ":20", ":30", ":40"]
