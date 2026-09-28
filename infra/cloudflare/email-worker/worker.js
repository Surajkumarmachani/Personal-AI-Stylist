/**
 * Cloudflare Email Worker: receives order emails for orders-<token>@<domain>
 * and posts them to the API's /inbound/email webhook.
 *
 * WHY THIS EXISTS: Cloudflare Email Routing receives mail for free, which keeps
 * the whole stack inside its ~$35/month budget (SendGrid's inbound parse now
 * needs a paid plan). The fields posted are the same ones SendGrid's Inbound
 * Parse sends (to, from, subject, html, text, headers, envelope), so the API
 * does not care which of the two is in front of it.
 *
 * The From posted is the HEADER From (the store, for a Gmail auto-forward), not
 * the envelope sender, which Gmail rewrites to the user's own address.
 */
import PostalMime from "postal-mime";

export default {
  async email(message, env) {
    const parsed = await PostalMime.parse(message.raw);

    const from = parsed.from?.address
      ? `${parsed.from.name ? `${parsed.from.name} ` : ""}<${parsed.from.address}>`
      : message.from;

    const form = new FormData();
    form.set("to", message.to);
    form.set("from", from);
    form.set("subject", parsed.subject ?? "");
    form.set("html", parsed.html ?? "");
    form.set("text", parsed.text ?? "");
    form.set("headers", parsed.messageId ? `Message-ID: ${parsed.messageId}\n` : "");
    form.set("envelope", JSON.stringify({ to: [message.to], from: message.from }));

    const response = await fetch(env.INBOUND_URL, { method: "POST", body: form });
    if (!response.ok) {
      // Throwing makes delivery fail, so the sending server retries later
      // instead of the email being silently lost while the API is down.
      throw new Error(`inbound webhook returned ${response.status}`);
    }
  },
};
