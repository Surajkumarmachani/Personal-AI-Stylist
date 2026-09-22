import { OCCASION_IMAGES } from "./occasion-images";

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
export type Occasion = { id: string; ask: string; title: string; sub: string; img: string };

/* The tile photographs live in `public/occasions/<id>.jpg`, one per occasion,
 * named by taxonomy id so a tile cannot point at the wrong picture.
 *
 * REAL PHOTOGRAPHS, not generated ones, from Wikimedia Commons and Openverse
 * (CC-licensed). Every one was looked at before it shipped, which is not
 * ceremony: the first pass picked a firework in a farmyard for `festival_day`,
 * an empty airport walkway for `travel_day` and a man in a hat for `mehendi`.
 * They are cropped square at 520px — the grid renders ~260px, so this covers
 * 2x displays and nothing more; twelve full-size photos would outweigh the
 * rest of the page.
 *
 * ATTRIBUTION IS OUTSTANDING. CC BY and CC BY-SA require crediting the
 * author, and `public/occasions/CREDITS.md` records what is known so far.
 * That has to be completed before this is served to anyone but you. */

export const OCCASIONS: Occasion[] = [
  { img: OCCASION_IMAGES.wedding_ceremony, id: "wedding_ceremony", ask: "wedding", title: "Wedding", sub: "Guest" },
  { img: OCCASION_IMAGES.party_night, id: "party_night", ask: "party", title: "Party", sub: "& night out" },
  { img: OCCASION_IMAGES.office_casual, id: "office_casual", ask: "office", title: "Office", sub: "& business" },
  { img: OCCASION_IMAGES.dinner_date, id: "dinner_date", ask: "dinner date", title: "Date", sub: "Night" },
  { img: OCCASION_IMAGES.festival_day, id: "festival_day", ask: "Diwali", title: "Festive", sub: "& traditional" },
  { img: OCCASION_IMAGES.travel_day, id: "travel_day", ask: "travel", title: "Travel", sub: "& vacation" },
  { img: OCCASION_IMAGES.casual_outing, id: "casual_outing", ask: "casual", title: "Casual", sub: "Everyday" },
  { img: OCCASION_IMAGES.interview, id: "interview", ask: "interview", title: "Interview", sub: "Formal" },
  { img: OCCASION_IMAGES.workout, id: "workout", ask: "gym", title: "Gym", sub: "& activewear" },
  { img: OCCASION_IMAGES.mehendi, id: "mehendi", ask: "mehendi", title: "Mehendi", sub: "& haldi" },
  { img: OCCASION_IMAGES.sangeet, id: "sangeet", ask: "sangeet", title: "Sangeet", sub: "Celebration" },
  { img: OCCASION_IMAGES.temple_visit, id: "temple_visit", ask: "temple", title: "Temple", sub: "Visit" },
];
