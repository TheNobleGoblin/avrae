import ast
import copy
import re
from math import ceil, floor

import simpleeval
from simpleeval import DEFAULT_NAMES, EvalWithCompoundTypes, IterableTooLong, SimpleEval

from cogs5e.funcs.dice import roll
from cogs5e.models.errors import EvaluationError, FunctionRequiresCharacter, InvalidArgument
from .combat import SimpleCombat
from .functions import DEFAULT_FUNCTIONS, DEFAULT_OPERATORS
from .helpers import MAX_ITER_LENGTH, SCRIPTING_RE, get_uvars, update_uvars

if 'format_map' not in simpleeval.DISALLOW_METHODS:
    simpleeval.DISALLOW_METHODS.append('format_map')


class MathEvaluator(SimpleEval):
    """Evaluator with basic math functions exposed."""
    MATH_FUNCTIONS = {'ceil': ceil, 'floor': floor, 'max': max, 'min': min, 'round': round}

    def __init__(self, operators=None, functions=None, names=None):
        if operators is None:
            operators = DEFAULT_OPERATORS.copy()
        if functions is None:
            functions = DEFAULT_FUNCTIONS.copy()
        if names is None:
            names = DEFAULT_NAMES.copy()
        super(MathEvaluator, self).__init__(operators, functions, names)

    @classmethod
    def with_character(cls, character, spell_override=None):
        names = {}
        names.update(character.get_cvars())
        names.update(character.get_stat_vars())
        names['spell'] = spell_override or (character.get_spell_ab() - character.get_prof_bonus())
        return cls(names=names)

    def parse(self, string):
        """Parses a dicecloud-formatted string (evaluating text in {})."""
        return re.sub(r'(?<!\\){(.+?)}', lambda m: str(self.eval(m.group(1))), string)


class ScriptingEvaluator(EvalWithCompoundTypes):
    """Evaluator with compound types, comprehensions, and assignments exposed."""

    def __init__(self, ctx, operators=None, functions=None, names=None):
        if operators is None:
            operators = DEFAULT_OPERATORS.copy()
        if functions is None:
            functions = DEFAULT_FUNCTIONS.copy()
        if names is None:
            names = DEFAULT_NAMES.copy()
        super(ScriptingEvaluator, self).__init__(operators, functions, names)

        self.nodes.update({
            ast.JoinedStr: self._eval_joinedstr,  # f-string
            ast.FormattedValue: self._eval_formattedvalue,  # things in f-strings
            ast.ListComp: self._eval_listcomp,
            ast.SetComp: self._eval_setcomp,
            ast.DictComp: self._eval_dictcomp,
            ast.comprehension: self._eval_comprehension
        })

        self.functions.update(  # character-only functions
            get_cc=self.needs_char, set_cc=self.needs_char, get_cc_max=self.needs_char,
            get_cc_min=self.needs_char, mod_cc=self.needs_char,
            cc_exists=self.needs_char, create_cc_nx=self.needs_char,
            get_slots=self.needs_char, get_slots_max=self.needs_char, set_slots=self.needs_char,
            use_slot=self.needs_char,
            get_hp=self.needs_char, set_hp=self.needs_char, mod_hp=self.needs_char, hp_str=self.needs_char,
            get_temphp=self.needs_char, set_temphp=self.needs_char,
            set_cvar=self.needs_char, delete_cvar=self.needs_char, set_cvar_nx=self.needs_char,
            get_raw=self.needs_char
        )

        self.functions.update(
            set=self.set_value, exists=self.exists, combat=self.combat,
            get_gvar=self.get_gvar,
            set_uvar=self.set_uvar, delete_uvar=self.delete_uvar, set_uvar_nx=self.set_uvar_nx,
            uvar_exists=self.uvar_exists,
            chanid=self.chanid, servid=self.servid,
            get=self.get
        )

        self.assign_nodes = {
            ast.Name: self._assign_name,
            ast.Tuple: self._assign_tuple,
            ast.Subscript: self._assign_subscript
        }

        self._loops = 0
        self._cache = {
            "gvars": {},
            "uvars": {}
        }

        self.ctx = ctx
        self.character_changed = False
        self.combat_changed = False
        self.uvars_changed = set()

    @classmethod
    async def new(cls, ctx):
        inst = cls(ctx)
        uvars = await get_uvars(ctx)
        inst.names.update(uvars)
        inst._cache['uvars'].update(uvars)
        inst._cache['combat'] = await SimpleCombat.from_ctx(ctx)
        return inst

    async def with_character(self, character):
        self.names.update(character.get_cvars())
        self.names.update(character.get_stat_vars())
        self.names['spell'] = character.get_spell_ab() - character.get_prof_bonus()
        self.names['color'] = hex(character.get_color())[2:]
        self.names["currentHp"] = character.get_current_hp()

        if 'combat' in self._cache and self._cache['combat']:
            self._cache['combat'].func_set_character(character)

        self._cache['character'] = character

        # define character-specific functions
        def get_cc(name):
            return character.get_consumable_value(name)

        def get_cc_max(name):
            return character.evaluate_cvar(character.get_consumable(name).get('max', str(2 ** 32 - 1)))

        def get_cc_min(name):
            return character.evaluate_cvar(character.get_consumable(name).get('min', str(-(2 ** 32))))

        def set_cc(name, value: int, strict=False):
            character.set_consumable(name, value, strict)
            self.character_changed = True

        def mod_cc(name, val: int, strict=False):
            return set_cc(name, get_cc(name) + val, strict)

        def delete_cc(name):
            character.delete_consumable(name)
            self.character_changed = True

        def create_cc_nx(name: str, minVal: str = None, maxVal: str = None, reset: str = None,
                         dispType: str = None):
            if not name in character.get_all_consumables():
                character.create_consumable(name, minValue=minVal, maxValue=maxVal, reset=reset, displayType=dispType)
                self.character_changed = True

        def cc_exists(name):
            return name in character.get_all_consumables()

        def cc_str(name):
            counter = character.get_consumable(name)
            _max = counter.get('max')
            val = str(counter.get('value', 0))
            if counter.get('type') == 'bubble':
                if _max is not None:
                    _max = character.evaluate_cvar(_max)
                    numEmpty = _max - counter.get('value', 0)
                    filled = '\u25c9' * counter.get('value', 0)
                    empty = '\u3007' * numEmpty
                    val = f"{filled}{empty}"
            else:
                if _max is not None:
                    _max = character.evaluate_cvar(_max)
                    val = f"{counter.get('value')} / {_max}"
            return val

        def get_slots(level: int):
            return character.get_remaining_slots(level)

        def get_slots_max(level: int):
            return character.get_max_spellslots(level)

        def slots_str(level: int):
            return character.get_remaining_slots_str(level).strip()

        def set_slots(level: int, value: int):
            character.set_remaining_slots(level, value)
            self.character_changed = True

        def use_slot(level: int):
            character.use_slot(level)
            self.character_changed = True

        def get_hp():
            return character.get_current_hp() - character.get_temp_hp()

        def set_hp(val: int):
            character.set_hp(val, True)
            self.character_changed = True

        def mod_hp(val: int, overflow: bool = True):
            if not overflow:
                return set_hp(min(character.get_current_hp() + val, character.get_max_hp()))
            else:
                return set_hp(character.get_current_hp() + val)

        def hp_str():
            return character.get_hp_str()

        def get_temphp():
            return character.get_temp_hp()

        def set_temphp(val: int):
            character.set_temp_hp(val)
            self.character_changed = True

        def set_cvar(name, val: str):
            character.set_cvar(name, val)
            self.names[name] = str(val)
            self.character_changed = True

        def set_cvar_nx(name, val: str):
            if not name in character.get_cvars():
                set_cvar(name, val)

        def delete_cvar(name):
            if name in character.get_cvars():
                del character.get_cvars()[name]
                self.character_changed = True

        def get_raw():
            return copy.copy(character.character)

        self.functions.update(
            get_cc=get_cc, set_cc=set_cc, get_cc_max=get_cc_max, get_cc_min=get_cc_min, mod_cc=mod_cc,
            delete_cc=delete_cc, cc_exists=cc_exists, create_cc_nx=create_cc_nx, cc_str=cc_str,
            get_slots=get_slots, get_slots_max=get_slots_max, set_slots=set_slots, use_slot=use_slot,
            slots_str=slots_str,
            get_hp=get_hp, set_hp=set_hp, mod_hp=mod_hp, hp_str=hp_str,
            get_temphp=get_temphp, set_temphp=set_temphp,
            set_cvar=set_cvar, delete_cvar=delete_cvar, set_cvar_nx=set_cvar_nx,
            get_raw=get_raw
        )

        return self

    async def run_commits(self):
        if self.character_changed and 'character' in self._cache:
            await self._cache['character'].commit(self.ctx)
        if self.combat_changed and 'combat' in self._cache and self._cache['combat']:
            await self._cache['combat'].func_commit()
        if self.uvars_changed and 'uvars' in self._cache and self._cache['uvars']:
            await update_uvars(self.ctx, self._cache['uvars'], self.uvars_changed)

    # helpers
    def needs_char(self, *args, **kwargs):
        raise FunctionRequiresCharacter()  # no. bad.

    def set_value(self, name, value):
        self.names[name] = value

    def exists(self, name):
        return name in self.names

    def combat(self):
        if not 'combat' in self._cache:
            return None
        self.combat_changed = True
        return self._cache['combat']

    def uvar_exists(self, name):
        return self.exists(name) and name in self._cache['uvars']

    def get_gvar(self, name):
        if name not in self._cache['gvars']:
            result = self.ctx.bot.mdb.gvars.delegate.find_one({"key": name})
            if result is None:
                return None
            self._cache['gvars'][name] = result['value']
        return self._cache['gvars'][name]

    def set_uvar(self, name, val: str):
        if any(c in name for c in '/()[]\\.^$*+?|{}'):
            raise InvalidArgument("Cvar contains invalid character.")
        self._cache['uvars'][name] = str(val)
        self.names[name] = str(val)
        self.uvars_changed.add(name)

    def set_uvar_nx(self, name, val: str):
        if not name in self.names:
            self.set_uvar(name, val)

    def delete_uvar(self, name):
        if name in self._cache['uvars']:
            del self._cache['uvars'][name]
            self.uvars_changed.add(name)

    def chanid(self):
        return str(self.ctx.channel.id)

    def servid(self):
        if self.ctx.guild:
            return str(self.ctx.guild.id)
        return None

    def get(self, name, default=None):
        if name in self.names:
            return self.names[name]
        return default

    # evaluation
    def parse(self, string, double_curly=None, curly=None, ltgt=None):
        """Parses a scripting string (evaluating text in {{}})."""
        ops = r"([-+*/().<>=])"

        def evalrepl(match):
            if match.group(1):  # {{}}
                double_func = double_curly or self.eval
                evalresult = double_func(match.group(1))
            elif match.group(2):  # <>
                if re.match(r'<a?([@#]|:.+:)[&!]{0,2}\d+>', match.group(0)):  # ignore mentions
                    return match.group(0)
                out = match.group(2)
                ltgt_func = ltgt or (lambda s: str(self.names.get(s, s)))
                evalresult = ltgt_func(out)
            elif match.group(3):  # {}
                varstr = match.group(3)

                def default_curly_func(s):
                    curlyout = ""
                    for substr in re.split(ops, s):
                        temp = substr.strip()
                        curlyout += str(self.names.get(temp, temp)) + " "
                    return str(roll(curlyout).total)

                curly_func = curly or default_curly_func
                evalresult = curly_func(varstr)
            else:
                evalresult = None
            return str(evalresult) if evalresult is not None else ''

        try:
            output = re.sub(SCRIPTING_RE, evalrepl, string)  # evaluate
        except Exception as ex:
            raise EvaluationError(ex)

        return output

    def eval(self, expr):  # allow for ast.Assign to set names
        """ evaluate an expression, using the operators, functions and
            names previously set up. """

        # set a copy of the expression aside, so we can give nice errors...

        self.expr = expr

        # and evaluate:
        expression = ast.parse(expr.strip()).body[0]
        if isinstance(expression, ast.Expr):
            return self._eval(expression.value)
        elif isinstance(expression, ast.Assign):
            return self._eval_assign(expression)
        else:
            raise TypeError("Unknown ast body type")

    # private magic
    def _eval_assign(self, node):
        names = node.targets[0]
        values = node.value
        self._assign(names, values)

    def _assign(self, names, values, eval_values=True):
        try:
            handler = self.assign_nodes[type(names)]
        except KeyError:
            raise TypeError(f"Assignment to {type(names).__name__} is not allowed")
        return handler(names, values, eval_values)

    def _assign_name(self, name, value, eval_value=True):
        if not isinstance(self.names, dict):
            raise TypeError("cannot set name: incorrect name type")
        else:
            if eval_value:
                value = self._eval(value)
            self.names[name.id] = value

    def _assign_tuple(self, names, values, eval_values=True):
        if not all(isinstance(n, ast.Name) for n in names.elts):
            raise TypeError("Assigning to multiple non-names via unpack is not allowed")
        names = [n.id for n in names.elts]  # turn ast into str
        if not isinstance(values, ast.Tuple):
            raise ValueError(f"unequal unpack: {len(names)} names, 1 value")
        if eval_values:
            values = [self._eval(n) for n in values.elts]  # get what we actually want to assign
        else:
            values = values.elts
        if not len(values) == len(names):
            raise ValueError(f"unequal unpack: {len(names)} names, {len(values)} values")
        else:
            if not isinstance(self.names, dict):
                raise TypeError("cannot set name: incorrect name type")
            else:
                for name, value in zip(names, values):
                    self.names[name] = value  # and assign it

    def _assign_subscript(self, name, value, eval_value=True):
        if eval_value:
            value = self._eval(value)

        container = self._eval(name.value)
        key = self._eval(name.slice)
        container[key] = value
        self._assign(name.value, container, eval_values=False)

    def _eval_joinedstr(self, node):
        return ''.join(str(self._eval(n)) for n in node.values)

    def _eval_formattedvalue(self, node):
        if node.format_spec:
            fmt = "{:" + self._eval(node.format_spec) + "}"
            return fmt.format(self._eval(node.value))
        else:
            return self._eval(node.value)

    def _eval_listcomp(self, node):
        return list(self._eval(node.elt) for generator in node.generators for _ in self._eval(generator))

    def _eval_setcomp(self, node):
        return set(self._eval(node.elt) for generator in node.generators for _ in self._eval(generator))

    def _eval_dictcomp(self, node):
        return {self._eval(node.key): self._eval(node.value) for generator in node.generators for _ in
                self._eval(generator)}

    def _eval_comprehension(self, node):
        iterable = self._eval(node.iter)
        if len(iterable) + self._loops > MAX_ITER_LENGTH:
            raise IterableTooLong("Execution limit exceeded: too many loops.")
        self._loops += len(iterable)
        for item in iterable:
            self._assign(node.target, item, False)
            if all(self._eval(stmt) for stmt in node.ifs):
                yield item


class SpellEvaluator(MathEvaluator):
    @classmethod
    def with_caster(cls, caster, spell_override=None):
        names = {'spell': spell_override or (caster.spellcasting.sab - caster.pb_from_level())}
        return cls(names=names)

    def parse(self, string, extra_names=None):
        """Parses a spell-formatted string (evaluating {{}} and replacing {} with rollstrings)."""
        original_names = None
        if extra_names:
            original_names = self.names.copy()
            self.names.update(extra_names)

        def evalrepl(match):
            if match.group(1):  # {{}}
                evalresult = self.eval(match.group(1))
            elif match.group(3):  # {}
                evalresult = self.names.get(match.group(3), match.group(0))
            else:
                evalresult = None
            return str(evalresult) if evalresult is not None else ''

        try:
            output = re.sub(SCRIPTING_RE, evalrepl, string)  # evaluate
        except Exception as ex:
            raise EvaluationError(ex)

        if original_names:
            self.names = original_names

        return output
