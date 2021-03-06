from bson import ObjectId

from cogs5e.models.errors import NoActiveBrew
from cogs5e.models.spell import Spell
from utils.functions import search_and_select


class Tome:
    def __init__(self, _id: ObjectId, name: str, owner: dict, editors: list, public: bool, active: list,
                 server_active: list, spells: list, image: str, desc: str, subscribers=None, **kwargs):
        if subscribers is None:
            subscribers = []
        self.id = _id
        self.name = name
        self.owner = owner
        self.editors = editors
        self.subscribers = subscribers
        self.public = public
        self.active = active
        self.server_active = server_active
        self.spells = spells
        self.image = image
        self.desc = desc

    @classmethod
    def from_dict(cls, raw):
        raw['spells'] = list(map(Spell.from_dict, raw['spells']))
        return cls(**raw)

    @classmethod
    async def from_id(cls, ctx, tome_id):
        tome = await ctx.bot.mdb.tomes.find_one({"_id": ObjectId(tome_id)})
        if tome is None:
            raise NoActiveBrew()
        return cls.from_dict(tome)

    @classmethod
    async def from_ctx(cls, ctx):
        active_tome = await ctx.bot.mdb.tomes.find_one({"active": str(ctx.author.id)})
        if active_tome is None:
            raise NoActiveBrew()
        return cls.from_dict(active_tome)

    def to_dict_no_spells(self):
        # spells = [s.to_dict() for s in self.spells]
        return {'name': self.name, 'owner': self.owner, 'editors': self.editors, 'public': self.public,
                'active': self.active, 'server_active': self.server_active, 'image': self.image,
                'desc': self.desc,  # end v1
                'subscribers': self.subscribers}

    async def commit(self, ctx):
        """Writes a tome object to the database. Does not modify spells."""
        data = self.to_dict_no_spells()

        await ctx.bot.mdb.tomes.update_one(
            {"_id": self.id}, {"$set": data}
        )

    async def set_active(self, ctx):
        await ctx.bot.mdb.tomes.update_many(
            {"active": str(ctx.author.id)},
            {"$pull": {"active": str(ctx.author.id)}}
        )
        await ctx.bot.mdb.tomes.update_one(
            {"_id": self.id},
            {"$push": {"active": str(ctx.author.id)}}
        )

    async def toggle_server_active(self, ctx):
        """
        Toggles whether the tome should be active on the contextual server.
        :param ctx: Context
        :return: Whether the tome is now active on the server.
        """
        data = await ctx.bot.mdb.tomes.find_one({"_id": self.id}, ["server_active"])
        server_active = data.get('server_active', [])
        if str(ctx.guild.id) in server_active:
            server_active.remove(str(ctx.guild.id))
        else:
            server_active.append(str(ctx.guild.id))
        await ctx.bot.mdb.tomes.update_one(
            {"_id": self.id},
            {"$set": {"server_active": server_active}}
        )
        return str(ctx.guild.id) in server_active

    @staticmethod
    def view_query(user_id):
        """Returns the MongoDB query to find all documents a user can set active."""
        return {"$or": [
            {"owner.id": user_id},
            {"editors.id": user_id},
            {"$and": [
                {"subscribers.id": user_id},
                {"public": True}
            ]}
        ]}


async def select_tome(ctx, name):
    available_tome_names = await ctx.bot.mdb.tomes.find(
        Tome.view_query(str(ctx.author.id)),
        ['name', '_id']
    ).to_list(None)

    if not available_tome_names:
        raise NoActiveBrew()

    result = await search_and_select(ctx, available_tome_names, name, lambda p: p['name'])
    final_tome = await ctx.bot.mdb.tomes.find_one({"_id": result['_id']})

    return Tome.from_dict(final_tome)
