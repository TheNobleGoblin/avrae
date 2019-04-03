import re
import shlex

from utils.argparser import argquote

SCRIPTING_RE = re.compile(r'(?<!\\)(?:(?:{{(.+?)}})|(?:<([^\s]+)>)|(?:(?<!{){(.+?)}))')
MAX_ITER_LENGTH = 10000


async def get_uvars(ctx):
    uvars = {}
    async for uvar in ctx.bot.mdb.uvars.find({"owner": str(ctx.author.id)}):
        uvars[uvar['name']] = uvar['value']
    return uvars


async def set_uvar(ctx, name, value):
    await ctx.bot.mdb.uvars.update_one(
        {"owner": str(ctx.author.id), "name": name},
        {"$set": {"value": value}},
        True)


async def update_uvars(ctx, uvar_dict, changed=None):
    if changed is None:
        for name, value in uvar_dict.items():
            await set_uvar(ctx, name, value)
    else:
        for name in changed:
            if name in uvar_dict:
                await set_uvar(ctx, name, uvar_dict[name])
            else:
                await ctx.bot.mdb.uvars.delete_one({"owner": str(ctx.author.id), "name": name})


async def get_gvar_values(ctx):
    gvars = {}
    async for gvar in ctx.bot.mdb.gvars.find():
        gvars[gvar['key']] = gvar['value']
    return gvars


async def get_aliases(ctx):
    aliases = {}
    async for alias in ctx.bot.mdb.aliases.find({"owner": str(ctx.author.id)}):
        aliases[alias['name']] = alias['commands']
    return aliases


async def get_servaliases(ctx):
    servaliases = {}
    async for servalias in ctx.bot.mdb.servaliases.find({"server": str(ctx.guild.id)}):
        servaliases[servalias['name']] = servalias['commands']
    return servaliases


async def get_snippets(ctx):
    snippets = {}
    async for snippet in ctx.bot.mdb.snippets.find({"owner": str(ctx.author.id)}):
        snippets[snippet['name']] = snippet['snippet']
    return snippets


async def get_servsnippets(ctx):
    servsnippets = {}
    if ctx.guild:
        async for servsnippet in ctx.bot.mdb.servsnippets.find({"server": str(ctx.guild.id)}):
            servsnippets[servsnippet['name']] = servsnippet['snippet']
    return servsnippets


async def parse_snippets(args: str, ctx) -> str:
    """
    Parses user and server snippets.
    :param args: The string to parse. Will be split automatically
    :param ctx: The Context.
    :return: The string, with snippets replaced.
    """
    tempargs = shlex.split(args)
    snippets = await get_servsnippets(ctx)
    snippets.update(await get_snippets(ctx))
    for index, arg in enumerate(tempargs):  # parse snippets
        snippet_value = snippets.get(arg)
        if snippet_value:
            tempargs[index] = snippet_value
        elif ' ' in arg:
            tempargs[index] = argquote(arg)
    return " ".join(tempargs)
