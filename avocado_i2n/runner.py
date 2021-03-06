"""

SUMMARY
------------------------------------------------------
Specialized test runner for the plugin.

Copyright: Intra2net AG


INTERFACE
------------------------------------------------------

"""

import os
import logging
import signal
from multiprocessing import SimpleQueue

from avocado.core.runner import TestRunner

from . import params_parser as param
from .cartesian_graph import TestGraph, TestNode


class CartesianRunner(TestRunner):
    """Test runner for Cartesian graph traversal."""

    """running functionality"""
    def run_test_node(self, node):
        """
        A wrapper around the inherited :py:meth:`run_test`.

        :param node: test node to run
        :type node: :py:class:`TestNode`
        :return: run status of :py:meth:`run_test`
        :rtype: bool

        This is a simple wrapper to provide some default arguments
        for simplicity of invocation.
        """
        return self.run_test(node.get_test_factory(self.job), SimpleQueue(), set())

    def run_traversal(self, graph, param_str):
        """
        Run all user and system defined tests optimizing the setup reuse and
        minimizing the repetition of demanded tests.

        :param graph: test graph to traverse
        :type graph: :py:class:`TestGraph`
        :param str param_str: block of command line parameters
        :raises: :py:class:`AssertionError` if some traversal assertions are violated

        The highest priority is at the setup tests (parents) since the test cannot be
        run without the required setup, then the current test, then a single child of
        its children (DFS), and finally the other children (tests that can benefit from
        the fact that this test/setup was done) followed by the other siblings (tests
        benefiting from its parent/setup.

        Of course all possible children are restricted by the user-defined "only" and
        the number of internal test nodes is minimized for achieving this goal.
        """
        shared_roots = graph.get_nodes_by("name", "(\.|^)0scan(\.|^)")
        assert len(shared_roots) == 1, "There can be only exactly one root node"
        root = shared_roots[0]

        if logging.getLogger('graph').level <= logging.DEBUG:
            traverse_dir = os.path.join(self.job.logdir, "graph_traverse")
            if not os.path.exists(traverse_dir):
                os.mkdir(traverse_dir)
            step = 0

        traverse_path = [root]
        while not root.is_cleanup_ready():
            next = traverse_path[-1]
            if len(traverse_path) > 1:
                previous = traverse_path[-2]
            else:
                # since the loop is discontinued if len(traverse_path) == 0 or root.is_cleanup_ready()
                # a valid current node with at least one child is guaranteed
                traverse_path.append(next.pick_next_child())
                continue

            logging.debug("At test node %s which is %sready with setup, %sready with cleanup,"
                          " should %srun, and should %sbe cleaned", next.params["shortname"],
                          "not " if not next.is_setup_ready() else "",
                          "not " if not next.is_cleanup_ready() else "",
                          "not " if not next.should_run else "",
                          "not " if not next.should_clean else "")
            logging.debug("Current traverse path/stack:%s",
                          "\n".join([n.params["shortname"] for n in traverse_path]))
            # if previous in path is the child of the next, then the path is reversed
            # looking for setup so if the next is setup ready and already run, remove
            # the previous' reference to it and pop the current next from the path
            if previous in next.cleanup_nodes or previous in next.visited_cleanup_nodes:

                if next.is_setup_ready():
                    self._traverse_test_node(graph, next, param_str)
                    previous.visit_node(next)
                    traverse_path.pop()
                else:
                    # inverse DFS
                    traverse_path.append(next.pick_next_parent())
            elif previous in next.setup_nodes or previous in next.visited_setup_nodes:

                # stop if test is not a setup leaf since parents have higher priority than children
                if not next.is_setup_ready():
                    traverse_path.append(next.pick_next_parent())
                    continue
                else:
                    self._traverse_test_node(graph, next, param_str)

                if next.is_cleanup_ready():
                    self._reverse_test_node(graph, next, param_str)
                    for setup in next.visited_setup_nodes:
                        setup.visit_node(next)
                    traverse_path.pop()
                    graph.report_progress()
                else:
                    # normal DFS
                    traverse_path.append(next.pick_next_child())
            else:
                raise AssertionError("Discontinuous path in the test dependency graph detected")

            if logging.getLogger('graph').level <= logging.DEBUG:
                step += 1
                graph.visualize(traverse_dir, step)

    def run_suite(self, test_suite, _variant, _timeout=0,
                  _replay_map=None, _execution_order=None):
        """
        Run one or more tests and report with test result.

        :param test_suite: a list of tests to run
        :type test_suite: [(type, {str, str})]
        :param variants: varianter iterator to produce test params
        :type variants: :py:class:`avocado.core.varianter.Varianter`
        :param int timeout: maximum amount of time (in seconds) to execute
        :param replay_map: optional list to override test class based on test index
        :type replay_map: [None or type]
        :param str execution_order: Mode in which we should iterate through tests
                                    and variants (if None will default to
                                    :py:attr:`DEFAULT_EXECUTION_ORDER`
        :returns: a set with types of test failures
        :rtype: :py:class:`set`
        """
        graph = self._graph_from_suite(test_suite)
        summary = set()
        if self.job.sysinfo is not None:
            self.job.sysinfo.start_job_hook()
        param_str = self.job.args.param_str

        try:
            graph.visualize(self.job.logdir)
            self.run_traversal(graph, param_str)
        except KeyboardInterrupt:
            TEST_LOG.error('Job interrupted by ctrl+c.')
            summary.add('INTERRUPTED')

        if self.job.sysinfo is not None:
            self.job.sysinfo.end_job_hook()
        self.result.end_tests()
        self.job.funcatexit.run()
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        return summary

    """custom nodes"""
    def run_scan_node(self, graph, param_str, tag=""):
        """
        Run the set of tests necessary for starting test traversal.

        :param graph: test graph to run scan node from
        :type graph: :py:class:`TestGraph`
        :param str param_str: block of command line parameters
        :param str tag: extra name identifier for the test to be run
        """
        # HACK: pass the constructed graph to the test using static attribute hack
        # since there is absolutely no sane way to pass through the cloud of imports
        # before executing a VT test (could be improved later on)
        TestGraph.REFERENCE = graph

        nodes = graph.get_nodes_by(param_key="name", param_val="(\.|^)0scan(\.|^)")
        assert len(nodes) == 1, "There can only be one shared root"
        test_node = nodes[0]
        self.run_test_node(test_node)

        graph.load_setup_list(self.job.logdir)
        for node in graph.nodes:
            self.result.cancelled += 1 if not node.should_run else 0

    def run_create_node(self, graph, object_name, param_str, tag=""):
        """
        Run the set of tests necessary for creating a given test object.

        :param graph: test graph to run create node from
        :type graph: :py:class:`TestGraph`
        :param str object_name: name of the test object to be created
        :param str param_str: block of command line parameters
        :param str tag: extra name identifier for the test to be run
        """
        objects = graph.get_objects_by(param_key="main_vm", param_val="^"+object_name+"$")
        assert len(objects) == 1, "Test object %s not existing or unique in: %s" % (object_name, objects)
        test_object = objects[0]
        nodes = graph.get_nodes_by("name", "(\.|^)0root(\.|$)",
                                   subset=graph.get_nodes_by("vms", "(^|\s)%s($|\s)" % test_object.name))
        assert len(nodes) == 1, "There can only be one root for %s" % object_name
        test_node = nodes[0]
        # implementation of object creation needs a separate parser
        create_parser = param.update_parser(test_object.parser,
                                            ovrwrt_dict={"vm_action": "set",
                                                         "skip_image_processing": "yes"},
                                            ovrwrt_str=param.re_str("manage.unchanged",
                                                                    param_str, tag, True),
                                            ovrwrt_base_file="sets.cfg",
                                            ovrwrt_file=param.tests_ovrwrt_file)
        self.run_test_node(TestNode(test_node.name, create_parser, test_node.objects))

    def run_install_node(self, graph, object_name, param_str, tag=""):
        """
        Run the set of tests necessary for installing a given test object.

        :param graph: test graph to run install node from
        :type graph: :py:class:`TestGraph`
        :param str object_name: name of the test object to be installed
        :param str param_str: block of command line parameters
        :param str tag: extra name identifier for the test to be run
        :raises: :py:class:`NotImplementedError` if using incompatible installation variant
        """
        objects = graph.get_objects_by(param_key="main_vm", param_val="^"+object_name+"$")
        assert len(objects) == 1, "Test object %s not existing or unique in: %s" % (object_name, objects)
        test_object = objects[0]
        nodes = graph.get_nodes_by("name", "(\.|^)(install|unattended_install)(\.|$)",
                                   subset=graph.get_nodes_by("vms", "(^|\s)%s($|\s)" % test_object.name))
        assert len(nodes) == 1, "There can only be one root for %s" % object_name
        test_node = nodes[0]

        logging.info("Configuring installation for %s", test_object.name)
        install_parser = param.update_parser(test_object.parser,
                                             ovrwrt_str=param.re_str("0preinstall", param_str, tag, True),
                                             ovrwrt_base_file="sets.cfg",
                                             ovrwrt_file=param.tests_ovrwrt_file)
        # some parameters from the install configuration have to be used for decision about install tests
        install_params = param.peek(param.copy_parser(install_parser))
        status = self.run_test_node(TestNode("0p", install_parser, test_node.objects))

        if status:
            logging.info("Installing virtual machine %s", test_object.name)
            if install_params.get("configure_install", "stepmaker") == "unattended_install":
                if ".Fedora." in test_object.params["name"] or ".CentOS." in test_object.params["name"]:
                    ovrwrt_str = param.re_str("unattended_install.cdrom.extra_cdrom_ks", param_str, tag, True)
                elif ".Windows." in test_object.params["name"]:
                    ovrwrt_str = param.re_str("unattended_install.cdrom", param_str, tag, True)
                else:
                    raise NotImplementedError("Unattended install tests are only supported on Windows and Fedora/CentOS")
                ovrwrt_dict = {}
            else:
                ovrwrt_dict = {"type": install_params.get("configure_install", "stepmaker")}
                ovrwrt_str = param.re_str("install", param_str, tag, True)
            install_parser = param.update_parser(test_object.parser,
                                                 ovrwrt_dict=ovrwrt_dict,
                                                 ovrwrt_str=ovrwrt_str,
                                                 ovrwrt_base_file="sets.cfg",
                                                 ovrwrt_file=param.tests_ovrwrt_file)
            self.run_test_node(TestNode("0q", install_parser, test_node.objects))

    """internals"""
    def _traverse_test_node(self, graph, test_node, param_str):
        """Run a single test according to user defined policy and state availability."""
        # ephemeral setup can get lost and if so must be repeated
        if not test_node.should_run and test_node.is_ephemeral() and not test_node.is_cleanup_ready():
            for test_object in test_node.objects:
                object_name = test_object.name
                object_params = test_node.params.object_params(object_name)
                # if previous state is not known keep behavior assuming that the user knows what they are doing
                if object_params.get("set_state") != test_object.current_state != "unknown":
                    logging.debug("Re-running ephemeral setup %s since %s state was switched to %s",
                                  test_node.params["shortname"], test_object.name, test_object.current_state)
                    test_node.should_run = True
                    break

        if test_node.should_run:
            if test_node.is_scan_node():
                logging.debug("Test run started from the shared root")
                self.run_scan_node(graph, param_str)

            # the primary setup nodes need special treatment
            elif test_node.is_install_node() or test_node.is_create_node():
                setup_str = param_str
                if test_node.is_create_node():
                    setup_str += param.dict_to_str({"set_state": "root", "set_type": "offline"})
                    self.run_create_node(graph, test_node.params.get("vms", ""), setup_str)
                elif test_node.is_install_node():
                    self.run_install_node(graph, test_node.params.get("vms", ""), setup_str)

            else:
                # finally, good old running of an actual test
                self.run_test_node(test_node)

            for test_object in test_node.objects:
                object_name = test_object.name
                object_params = test_node.params.object_params(object_name)
                if object_params.get("set_state") is not None and object_params.get("set_state") != "":
                    test_object.current_state = object_params.get("set_state")
            test_node.should_run = False
        else:
            logging.debug("Skipping test %s", test_node.params["shortname"])

    def _reverse_test_node(self, graph, test_node, param_str):
        """
        Clean up any states that could be created by this node (will be skipped
        by default but the states can be removed with "unset_mode=f.").
        """
        if test_node.should_clean:
            if test_node.is_shared_root():
                logging.debug("Test run ended at the shared root")
            else:
                for vm_name in test_node.params.objects("vms"):
                    vm_params = test_node.params.object_params(vm_name)
                    # avoid running any test unless the user really requires cleanup
                    if vm_params.get("unset_mode", "ri")[0] == "f" and vm_params.get("set_state"):
                        # NOTE: we are forcing the unset_mode to be the one defined for the test node because
                        # the unset manual step behaves differently now (all this extra complexity starts from
                        # the fact that it has different default value which is noninvasive
                        setup_str = param.dict_to_str({"unset_state": vm_params["set_state"],
                                                       "unset_type": vm_params.get("set_type", "offline"),
                                                       "unset_mode": vm_params.get("unset_mode", "ri")}) + param_str

                        objects = graph.get_objects_by(param_key="main_vm", param_val="^"+vm_name+"$")
                        assert len(objects) == 1, "Test object %s not existing or unique in: %s" % (vm_name, objects)
                        test_object = objects[0]
                        forward_parser = param.update_parser(test_object.parser,
                                                             ovrwrt_dict={"vm_action": "unset",
                                                                         "skip_image_processing": "yes"},
                                                             ovrwrt_str=param.re_str("manage.unchanged",
                                                                                     setup_str, "", True),
                                                             ovrwrt_base_file="sets.cfg",
                                                             ovrwrt_file=param.tests_ovrwrt_file)
                        self.run_test_node(TestNode("c" + test_node.name, forward_parser, [test_object]))

        else:
            logging.debug("The test %s doesn't leave any states to be cleaned up", test_node.params["shortname"])

    def _graph_from_suite(self, test_suite):
        """
        Restore a Cartesian graph from the digested list of test object factories.
        """
        # HACK: pass the constructed graph to the runner using static attribute hack
        # since the currently digested test suite contains factory arguments obtained
        # from an irreversible (information destructive) approach
        graph = TestGraph.REFERENCE

        # validate the test suite refers to the same test graph
        assert len(test_suite) == len(graph.nodes)
        for node1, node2 in zip(test_suite, graph.nodes):
            assert node1 == node2.get_test_factory()

        return graph
