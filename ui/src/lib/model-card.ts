// TD-5185: the read-only line under each tier in Settings. Formats the
// numbers the daemon sent; it never works out a price of its own.
import type { TierCard } from "./protocol";

function dollars(value: number): string {
	return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

/** "$2.00 in / $10.00 out per million, cache $0.20, 1048576 context". */
export function cardLine(card: TierCard): string {
	return (
		`${dollars(card.input_price)} in / ${dollars(card.output_price)} out per million, ` +
		`cache ${dollars(card.cache_read_price)}, ${card.context_window} context`
	);
}

/** What the last refresh did, or null when there was none this run. */
export function cardNote(card: TierCard): string | null {
	switch (card.card) {
		case null:
		case undefined:
			return null;
		case "ok":
			return "From the model card.";
		case "no_slug":
			return "No model named, so no card. Values are from config.yaml.";
		default:
			return `Card not used: ${card.detail ?? card.card}. Values are from config.yaml.`;
	}
}
