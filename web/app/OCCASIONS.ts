/** The occasion tiles.
 *
 * `id` is a REAL taxonomy occasion — `resolve_context` raises on anything
 * else, so a tile with an invented id would be a 400 the moment it is tapped.
 * `ask` is the phrase sent to /chat, which the lexicon in
 * `stylist_domain.intent` resolves back to the same occasion.
 *
 * The design shows six to nine tiles; the taxonomy defines eighteen. These are
 * the ones a person actually dresses for, ordered as the mockup orders them.
 */
export type Occasion = { id: string; ask: string; title: string; sub: string };

export const OCCASIONS: Occasion[] = [
  { id: "wedding_ceremony", ask: "wedding", title: "Wedding", sub: "Guest" },
  { id: "party_night", ask: "party", title: "Party", sub: "& night out" },
  { id: "office_casual", ask: "office", title: "Office", sub: "& business" },
  { id: "dinner_date", ask: "dinner date", title: "Date", sub: "Night" },
  { id: "festival_day", ask: "Diwali", title: "Festive", sub: "& traditional" },
  { id: "travel_day", ask: "travel", title: "Travel", sub: "& vacation" },
  { id: "casual_outing", ask: "casual", title: "Casual", sub: "Everyday" },
  { id: "interview", ask: "interview", title: "Interview", sub: "Formal" },
  { id: "workout", ask: "gym", title: "Gym", sub: "& activewear" },
  { id: "mehendi", ask: "mehendi", title: "Mehendi", sub: "& haldi" },
  { id: "sangeet", ask: "sangeet", title: "Sangeet", sub: "Celebration" },
  { id: "temple_visit", ask: "temple", title: "Temple", sub: "Visit" },
];
