"""Stable topic identifiers, hierarchical lookup and explicit legacy aliases."""
from dataclasses import dataclass
from types import MappingProxyType

TAXONOMY_VERSION = 'topics-v1.5'


@dataclass(frozen=True)
class Category:
    id: str
    display_name: str
    parent_id: str | None = None
    status: str = 'active'
    scope: str = ''


_DEFINITIONS = (
    ('entertainment', 'Entertainment', (('film','Movies'),('music','Music'),('television','Television'),('anime','Anime'))),
    ('celebrity_public_figures', 'People & Public Figures', (
        ('performers','Actors & Performers'),('musicians','Musicians & Singers'),
        ('creators','Content Creators'),('political_advisers','Political Advisers & Communications'),
        ('politicians','Politicians & Public Officials'),('coaches','Coaches & Sports Professionals'),
        ('comedians','Comedians'),('journalists','Journalists'),('writers','Writers'),('entrepreneurs','Entrepreneurs'),
        ('athletes','Athletes'),('photographers','Photographers & Artists'),
        ('scientists','Scientists & Researchers'),('health_professionals','Health Professionals'),
        ('legal_professionals','Legal Professionals'),('business_leaders','Business Leaders'),
        ('public_figures','Other Public Figures'))),
    ('arts_culture', 'Arts & Culture', (
        ('photography','Photography'),('visual_arts','Visual Arts'),('literature','Literature'),('performing_arts','Performing Arts'))),
    ('fashion', 'Fashion', (('clothing','Clothing'),('footwear','Footwear'),('accessories','Accessories'),('jewelry','Jewelry'))),
    ('home_living', 'Home & Living', (('appliances','Home Appliances'),('kitchen','Kitchen'),('furniture','Furniture'),('decor','Home Decor'))),
    ('technology', 'Technology', (('ai','AI'),('software','Software'),('internet_services','Internet Services'),('digital_platforms','Digital Platforms'),('gadgets','Gadgets'),('electronics','Electronics'))),
    ('gaming', 'Gaming', (('video_games','Video Games'),('hardware','Gaming Hardware'),('esports','Esports'))),
    ('beauty_personal_care', 'Beauty & Personal Care', (('skincare','Skincare'),('cosmetics','Cosmetics'),('haircare','Haircare'))),
    ('sports', 'Sports', (('athletes','Athletes'),('teams','Teams'),('competitions','Competitions'),('equipment','Equipment'))),
    ('food_beverage', 'Food & Beverage', (('food','Food'),('drinks','Drinks'),('cooking','Cooking'))),
    ('health_fitness', 'Health & Fitness', (('fitness','Fitness'),('wellness','Wellness'),('health','Health Topics'))),
    ('travel_lifestyle', 'Travel & Lifestyle', (('destinations','Destinations'),('experiences','Experiences'),('lifestyle','Lifestyle'))),
    ('business_finance', 'Business & Finance', (('companies','Companies'),('markets','Financial Markets'),('finance','Finance'))),
    ('news_politics', 'News & Politics', (('elections','Elections'),('government','Government'),('policy','Public Policy'),('current_events','Current Events'),('public_figures','Public Figures'))),
    ('organizations', 'Organizations', (('nonprofits','Non-profit Organizations'),('government','Government Organizations'),('sports','Sports Organizations'),('media','Media Organizations'),('communities','Communities'))),
    ('products_brands', 'Products & Brands', (('consumer_products','Consumer Products'),('software','Software Products'),('technology','Technology Brands'),('fashion','Fashion Brands'),('beauty','Beauty Brands'),('food','Food Brands'))),
    ('events', 'Events', (('historical','Historical Events'),('festivals','Festivals & Commemorations'),('disasters','Disasters'))),
    ('places_transportation', 'Places & Transportation', (('countries','Countries'),('cities','Cities'),('settlements','Settlements'),('geographic_locations','Geographic Locations'),('roads','Roads'),('ships','Ships'),('aircraft','Aircraft'),('monuments_memorials','Monuments & Memorials'),('transportation_systems','Transportation Systems'))),
    ('science_nature', 'Science & Nature', (('chemistry','Chemical Compounds'),('biology','Biology'),('animals','Animals'),('plants','Plants'),('concepts','Scientific Concepts'),('discoveries','Scientific Discoveries'),('natural_phenomena','Natural Phenomena'))),
    ('creative_works', 'Creative Works', (('books','Books'),('artwork','Artwork'))),
    ('law_society', 'Law & Society', (('crime_justice','Crime & Justice'),('legal_cases','Legal Cases'))),
    ('other', 'Other', ()),
    ('unclassified', 'Unclassified', ()),
)
_entries = {}
_NESTED_DEFINITIONS = {
    'technology.internet_services': (('video_platforms', 'Video Platforms'),),
}
_SCOPES = {
    'celebrity_public_figures': 'Identity and profession topics for verified people; occupation labels require entity evidence.',
    'celebrity_public_figures.public_figures': 'Other verified public people without a more specific supported role; never a fallback for unknown entities.',
    'news_politics.public_figures': 'People discussed in political or governance coverage; distinct from a general public-person identity group.',
    'celebrity_public_figures.athletes': 'People classified by athletic occupation; sports events and organizations belong in Sports.',
    'sports.athletes': 'Athletes as a sports coverage topic; use the people taxonomy for verified person occupations.',
    'business_finance.companies': 'Business entities and corporate activity, not the occupations of business leaders.',
    'technology': 'Technology topics, products, and industry; do not infer this topic from a person name alone.',
    'entertainment': 'Entertainment media works and programs; arts and cultural practices are separate.',
    'arts_culture': 'Cultural practices, artists, literature, and visual or performing arts.',
}
for parent, label, children in _DEFINITIONS:
    if parent in _entries:
        raise ValueError(f'duplicate taxonomy ID: {parent}')
    _entries[parent] = Category(parent, label, scope=_SCOPES.get(parent, ''))
    for child, child_label in children:
        item_id = f'{parent}.{child}'
        if item_id in _entries:
            raise ValueError(f'duplicate taxonomy ID: {item_id}')
        if item_id == 'news_politics.public_figures':
            child_label = 'Political Public Figures'
        elif item_id == 'celebrity_public_figures.public_figures':
            child_label = 'Other Public Figures'
        _entries[item_id] = Category(item_id, child_label, parent, scope=_SCOPES.get(item_id, ''))
for parent, descendants in _NESTED_DEFINITIONS.items():
    if parent not in _entries:
        raise ValueError(f'unknown nested taxonomy parent: {parent}')
    for child, child_label in descendants:
        item_id = f'{parent}.{child}'
        if item_id in _entries:
            raise ValueError(f'duplicate taxonomy ID: {item_id}')
        _entries[item_id] = Category(item_id, child_label, parent, scope=_SCOPES.get(item_id, ''))
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
    items = tuple(item for item in CATEGORIES.values() if item.parent_id == parent.id)
    if parent.parent_id is not None and not items:
        raise ValueError('Expected a category with child topics')
    return items


def map_legacy_categories(category_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Map explicit old labels without reclassifying or rewriting persisted data."""
    allowed = {'fashion', 'home_goods', 'other', 'unclassified'}
    if any(item not in allowed for item in category_ids):
        raise ValueError('Unknown legacy category ID')
    return tuple(dict.fromkeys(resolve(item).id for item in category_ids))
