"""Stable topic identifiers, hierarchical lookup and explicit legacy aliases."""
from dataclasses import dataclass
from types import MappingProxyType

TAXONOMY_VERSION = 'topics-v1.0'


@dataclass(frozen=True)
class Category:
    id: str
    display_name: str
    parent_id: str | None = None
    status: str = 'active'


_DEFINITIONS = (
    ('entertainment', 'Entertainment', (('film','Film'),('music','Music'),('television','Television'),('anime','Anime'))),
    ('celebrity_public_figures', 'Celebrity & Public Figures', (('performers','Actors / Performers'),('musicians','Musicians'),('creators','Content Creators'),('public_figures','Public Figures'))),
    ('fashion', 'Fashion', (('clothing','Clothing'),('footwear','Footwear'),('accessories','Accessories'),('jewelry','Jewelry'))),
    ('home_living', 'Home & Living', (('appliances','Home Appliances'),('kitchen','Kitchen'),('furniture','Furniture'),('decor','Home Decor'))),
    ('technology', 'Technology', (('ai','AI'),('software','Software'),('gadgets','Gadgets'),('electronics','Electronics'))),
    ('gaming', 'Gaming', (('video_games','Video Games'),('hardware','Gaming Hardware'),('esports','Esports'))),
    ('beauty_personal_care', 'Beauty & Personal Care', (('skincare','Skincare'),('cosmetics','Cosmetics'),('haircare','Haircare'))),
    ('sports', 'Sports', (('athletes','Athletes'),('teams','Teams'),('competitions','Competitions'),('equipment','Equipment'))),
    ('food_beverage', 'Food & Beverage', (('food','Food'),('drinks','Drinks'),('cooking','Cooking'))),
    ('health_fitness', 'Health & Fitness', (('fitness','Fitness'),('wellness','Wellness'),('health','Health Topics'))),
    ('travel_lifestyle', 'Travel & Lifestyle', (('destinations','Destinations'),('experiences','Experiences'),('lifestyle','Lifestyle'))),
    ('business_finance', 'Business & Finance', (('companies','Companies'),('markets','Financial Markets'),('finance','Finance'))),
    ('news_politics', 'News & Politics', (('elections','Elections'),('government','Government'),('policy','Public Policy'),('current_events','Current Events'))),
    ('other', 'Other', ()),
    ('unclassified', 'Unclassified', ()),
)
_entries = {}
for parent, label, children in _DEFINITIONS:
    if parent in _entries:
        raise ValueError(f'duplicate taxonomy ID: {parent}')
    _entries[parent] = Category(parent, label)
    for child, child_label in children:
        item_id = f'{parent}.{child}'
        if item_id in _entries:
            raise ValueError(f'duplicate taxonomy ID: {item_id}')
        _entries[item_id] = Category(item_id, child_label, parent)
CATEGORIES = MappingProxyType(_entries)
ALIASES = MappingProxyType({'home_goods': 'home_living'})


def resolve(category_id: str) -> Category:
    """Resolve stable ID or explicitly documented alias; never infer from labels."""
    canonical = ALIASES.get(category_id, category_id)
    try:
        return CATEGORIES[canonical]
    except KeyError:
        raise ValueError(f'Unknown category ID: {category_id}') from None


def children(parent_id: str) -> tuple[Category, ...]:
    parent = resolve(parent_id)
    if parent.parent_id is not None:
        raise ValueError('Expected a parent category')
    return tuple(item for item in CATEGORIES.values() if item.parent_id == parent.id)


def map_legacy_categories(category_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Map explicit old labels without reclassifying or rewriting persisted data."""
    allowed = {'fashion', 'home_goods', 'other', 'unclassified'}
    if any(item not in allowed for item in category_ids):
        raise ValueError('Unknown legacy category ID')
    return tuple(dict.fromkeys(resolve(item).id for item in category_ids))
