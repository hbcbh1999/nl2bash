#!/usr/bin/env python

"""
This file augments the AST generated by bashlex with single-command architecture and constraints.
It also performs some normalization on the command arguments.
"""

from __future__ import print_function
import re
import sys

# bashlex stuff
import ast, errors, tokenizer, parser
from bash import _DIGIT_RE, _NUM, is_option, head_commands

# TODO: add stdin & stdout types
simplified_bash_syntax = [
    "Command ::= SingleCommand | Pipe",
    "Pipe ::= Command '|' Command",
    "SingleCommand ::= HeadCommand [OptionList]",
    "OptionList ::= Option | OptionList",
    "Option ::= Flag [Argument] | LogicOp Option",
    "Argument ::= SingleArgument | CommandSubstitution | ProcessSubstitution",
    "CommandSubstitution ::= ` Command `",
    "ProcessSubstitution ::= <( Command ) | >( Command )"
]

arg_syntax = [
    "File",
    "Pattern",
    "Number",
    "NumberExp ::= -Number | +Number",
    "SizeExp ::= Number(k) | Number(M) | Number(G) | Number(T) | Number(P)",
    "TimeExp ::= Number(s) | Number(m) | Number(h) | Number(d) | Number(w)",
    # TODO: add fine-grained permission pattern
    "PermissionMode",
    "UserName",
    "GroupName",
    "Unknown"
]

unary_logic_operators = set(['!', '-not'])

binary_logic_operators = set([
    '-and',
    '-or',
    '||',
    '&&',
    '-o'
])

def is_unary_logic_op(w):
    return w in unary_logic_operators

def is_binary_logic_op(w):
    return w in binary_logic_operators

class Node(object):
    def __init__(self, parent=None, lsb=None, kind="", value=""):
        """
        :member kind: ['pipe',
                      'headcommand',
                      'logicop',
                      'flag',
                      'file', 'pattern', 'numberexp',
                      'sizeexp', 'timeexp', 'permexp',
                      'username', 'groupname', 'unknown',
                      'number', 'unit', 'op',
                      'commandsubstitution',
                      'processsubstitution'
                     ]
        :member value: string value of the node
        :member parent: pointer to parent node
        :member lsb: pointer to left sibling node
        :member children: list of child nodes
        """
        self.parent = parent
        self.lsb = lsb
        self.rsb = None
        self.kind = kind
        self.value = value
        self.children = []
        self.num_child = -1         # default value, permits arbitrary number of children
        self.children_types = []    # a list of allowed types for each child
                                    # a single-element list of allowed types for every child if self.num_child = -1
                                    # dummy field if self.num_child = 0

    def addChild(self, child):
        self.children.append(child)

    def getNumChildren(self):
        return len(self.chidren)

    def getRightChild(self):
        if len(self.children) >= 1:
            return self.children[-1]
        else:
            return None

    def getSecond2RightChild(self):
        if len(self.children) >= 2:
            return self.children[-2]
        else:
            return None

    def removeChild(self, child):
        self.children.remove(child)

    def removeChildByIndex(self, index):
        self.children.pop(index)

# syntax constraints for different kind of nodes
class ArgumentNode(Node):
    def __init__(self, kind="", value="", parent=None, lsb=None):
        super(ArgumentNode, self).__init__(parent, lsb, kind, value)
        self.num_child = 0

class UnaryLogicOpNode(Node):
    def __init__(self, value="", parent=None, lsb=None):
        super(UnaryLogicOpNode, self).__init__( parent, lsb, 'unarylogicop', value)
        self.num_child = 1
        self.children_types = [set('flag')]

class BinaryLogicOpNode(Node):
    def __init__(self, value="", parent=None, lsb=None):
        super(BinaryLogicOpNode, self).__init__(parent, lsb, 'binarylogicop', value)
        self.num_child = 2
        self.children_types = [set('flag'), set('flag')]

class PipelineNode(Node):
    def __init__(self, parent=None, lsb=None):
        super(PipelineNode, self).__init__(parent, lsb)
        self.kind = 'pipeline'
        self.children_types = [set(['headcommand'])]

class CommandSubstitutionNode(Node):
    def __init__(self, parent=None, lsb=None):
        super(CommandSubstitutionNode, self).__init__(parent, lsb)
        self.kind = "commandsubstitution"
        self.num_child = 1
        self.children_types = [set(['pipe', 'headcommand'])]

class ProcessSubstitutionNode(Node):
    def __init__(self, value, parent=None, lsb=None):
        super(ProcessSubstitutionNode, self).__init__(parent, lsb)
        self.kind = "processsubstitution"
        if value in ["<", ">"]:
            self.value = value
        else:
            raise ValueError("Value of a processsubstitution has to be '<' or '>'.")
        self.num_child = 1
        self.children_types = [set(['pipe', 'headcommand'])]

def pretty_print(node, depth):
    print("    " * depth + node.kind.upper() + '(' + node.value + ')')
    for child in node.children:
        pretty_print(child, depth+1)

def linear_print(node, str=''):
    pass

# normalize special command syntax
def special_command_normalization(cmd):
    ## the first argument of "tar" is always interpreted as an option
    tar_fix = re.compile(' tar \w')
    if cmd.startswith('tar'):
        cmd = ' ' + cmd
        for w in re.findall(tar_fix, cmd):
            cmd = cmd.replace(w, w.replace('tar ', 'tar -'))
        cmd = cmd.strip()
    return cmd

def normalize_ast(cmd, normalize_digits):
    """
    Convert the bashlex parse tree of a command into the normalized form.
    :param cmd: bash command to parse
    :param normalize_digits: replace all digits in the tree with the special _NUM symbol
    :return normalized_tree
    """

    cmd = cmd.replace('\n', ' ').strip()
    cmd = special_command_normalization(cmd)

    if not cmd:
        return None

    def attach_to_tree(node, parent):
        node.parent = parent
        node.lsb = parent.getRightChild()
        parent.addChild(node)
        if node.lsb:
            node.lsb.rsb = node

    def find_attach_point(node, attach_point):
        if not is_option(node.word):
            return attach_point
        if attach_point.kind == "flag":
            return attach_point.parent
        elif attach_point.kind == "headcommand":
            return attach_point
        else:
            print("Error: cannot decide where to attach flag node")
            print(node)
            sys.exit()

    def normalize_word(w, normalize_digits):
        return re.sub(_DIGIT_RE, _NUM, w) if normalize_digits and not is_option(w) else w

    def normalize_command(node, current):
        attach_point = current

        END_OF_OPTIONS = False
        END_OF_COMMAND = False

        unary_logic_ops = []
        binary_logic_ops = []

        # normalize atomic command
        for child in node.parts:
            if END_OF_COMMAND:
                attach_point = attach_point.parent
                if attach_point.kind == "flag":
                    attach_point = attach_point.parent
                elif attach_point.kind == "headcommand":
                    pass
                else:
                    print('Error: compound command detected.')
                    print(node)
                    sys.exit()
                END_OF_COMMAND = False
            if child.kind == 'word':
                if child.word == "--":
                    END_OF_OPTIONS = True
                elif child.word == ";":
                    # handle end of utility introduced by '-exec' and whatnots
                    END_OF_COMMAND = True
                elif child.word in unary_logic_operators:
                    attach_point = find_attach_point(child, attach_point)
                    norm_node = UnaryLogicOpNode(child.word)
                    attach_to_tree(norm_node, attach_point)
                    unary_logic_ops.append(norm_node)
                elif child.word in binary_logic_operators:
                    attach_point = find_attach_point(child, attach_point)
                    norm_node = BinaryLogicOpNode(child.word)
                    attach_to_tree(norm_node, attach_point)
                    binary_logic_ops.append(norm_node)
                elif child.word in head_commands:
                    if len(child.word) == (child.pos[1] - child.pos[0]):
                        # not inside quotation marks
                        normalize(child, attach_point, "headcommand")
                        attach_point = attach_point.getRightChild()
                elif is_option(child.word) and not END_OF_OPTIONS:
                    attach_point = find_attach_point(child, attach_point)
                    normalize(child, attach_point, "flag")
                    attach_point = attach_point.getRightChild()
                else:
                    #TODO: handle fine-grained argument types
                    normalize(child, attach_point, "argument")
            else:
                print("Error: unknown type of child of CommandNode")
                print(node)
                sys.exit()

        # process logic operators
        for node in unary_logic_ops:
            # change right sibling to child
            rsb = node.rsb
            assert(rsb != None)
            node.parent.removeChild(rsb)
            rsb.parent = node
            rsb.lsb = None
            rsb.rsb = None
            node.addChild(rsb)
            node.rsb = None

    def normalize(node, current, arg_type=""):
        # recursively normalize each subtree
        if not type(node) is ast.node:
            raise ValueError('type(node) is not ast.node')
        if node.kind == 'word':
            # assign fine-grained types
            if node.parts and node.parts[0].kind != "tilde":
                # Compound arguments
                # commandsubstitution, processsubstitution, parameter
                if node.parts[0].kind == "processsubstitution":
                    if '>' in node.word:
                        norm_node = ProcessSubstitutionNode('>')
                        attach_to_tree(norm_node, current)
                        for child in node.parts:
                            normalize(child, norm_node)
                    elif '<' in node.word:
                        norm_node = ProcessSubstitutionNode('<')
                        attach_to_tree(norm_node, current)
                        for child in node.parts:
                            normalize(child, norm_node)
                elif node.parts[0].kind == "commandsubstitution":
                    norm_node = CommandSubstitutionNode()
                    attach_to_tree(norm_node, current)
                    for child in node.parts:
                        normalize(child, norm_node)
                elif node.parts[0].kind == "parameter":
                    # if not node.parts[0].value.isdigit():
                    value = normalize_word(node.word, normalize_digits)
                    norm_node = ArgumentNode(kind=arg_type, value=value)
                    attach_to_tree(norm_node, current)
                else:
                    for child in node.parts:
                        normalize(child, current)
            else:
                value = normalize_word(node.word, normalize_digits)
                norm_node = ArgumentNode(kind=arg_type, value=value)
                attach_to_tree(norm_node, current)
        elif node.kind == "pipeline":
            norm_node = PipelineNode()
            attach_to_tree(norm_node, current)
            if len(node.parts) % 2 == 0:
                print("Error: pipeline node must have odd number of parts")
                print(node)
                sys.exit()
            for child in node.parts:
                if child.kind == "command":
                    normalize(child, norm_node)
                elif child.kind == "pipe":
                    pass
                else:
                    print("Error: unrecognized type of child of pipeline node")
                    print(node)
                    sys.exit()
        elif node.kind == "list":
            if len(node.parts) > 2:
                # multiple commands, not supported
                raise("Unsupported: list of length >= 2")
            else:
                for child in node.parts:
                    normalize(child, current)
        elif node.kind == "commandsubstitution" or \
             node.kind == "processsubstitution":
            normalize(node.command, current)
        elif node.kind == "command":
            normalize_command(node, current)
        elif hasattr(node, 'parts'):
            for child in node.parts:
                # skip current node
                normalize(child, current)
        elif node.kind == "operator":
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "parameter":
            # not supported
            raise ValueError("Unsupported: parameters")
        elif node.kind == "redirect":
            # not supported
            # if node.type == '>':
            #     parse(node.input, tokens)
            #     tokens.append('>')
            #     parse(node.output, tokens)
            # elif node.type == '<':
            #     parse(node.output, tokens)
            #     tokens.append('<')
            #     parse(node.input, tokens)
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "for":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "if":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "while":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "until":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "assignment":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "function":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "tilde":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "heredoc":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)

    try:
        tree = parser.parse(cmd)
    except tokenizer.MatchedPairError, e:
        print("Cannot parse: %s - MatchedPairError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except errors.ParsingError, e:
        print("Cannot parse: %s - ParsingError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except NotImplementedError, e:
        print("Cannot parse: %s - NotImplementedError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except IndexError, e:
        print("Cannot parse: %s - IndexError" % cmd.encode('utf-8'))
        # empty command
        return None
    except AttributeError, e:
        print("Cannot parse: %s - AttributeError" % cmd.encode('utf-8'))
        # not a bash command
        return None

    if len(tree) > 1:
        print("Doesn't support command with multiple root nodes: %s" % cmd.encode('utf-8'))
    normalized_tree = Node(kind="root", value="root")
    try:
        normalize(tree[0], normalized_tree)
    except ValueError as err:
        print("%s - %s" % (err.args[0], cmd.encode('utf-8')))
        return None

    return normalized_tree

if __name__ == "__main__":
    cmd = sys.argv[1]
    norm_tree = normalize_ast(cmd, True)
    pretty_print(norm_tree, 0)
    linear_print(norm_tree, '')



