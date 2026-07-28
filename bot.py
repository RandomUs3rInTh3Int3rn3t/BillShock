import os
import json
import logging
import re
import calendar
import asyncio
from typing import List, Optional
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Import our rate parser module
from meralco_parser import get_meralco_rates, RATES_JSON_PATH

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("meralco_bot")

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_CHANNEL_ID = os.getenv("ALLOWED_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Setup Bot Client with message content intent
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Setup Gemini AI Chatbot
gemini_model = None
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        for model_name in ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-pro"]:
            try:
                gemini_model = genai.GenerativeModel(model_name)
                logger.info(f"Successfully initialized Gemini AI model: {model_name}")
                break
            except Exception:
                continue
    except ImportError:
        logger.warning("google-generativeai package not installed. Install via requirements.txt.")
    except Exception as e:
        logger.error(f"Error initializing Gemini AI: {e}")

def get_system_context() -> str:
    """Build system context using current rates and appliance database (RAG)."""
    context_lines = [
        "You are BillShock AI, an expert, friendly assistant for Meralco electricity bills, appliance power usage, and energy-saving advice in the Philippines.",
        "Use Philippine Peso (₱ / PHP) for currency.",
        "Provide clear, helpful, concise answers formatted cleanly with Discord markdown (bold, bullet points, code blocks where helpful)."
    ]
    
    if os.path.exists(RATES_JSON_PATH):
        try:
            with open(RATES_JSON_PATH, "r", encoding="utf-8") as f:
                rates_data = json.load(f)
                if rates_data.get("success") and rates_data.get("data"):
                    context_lines.append("\n--- Live Meralco Rates Data ---")
                    if rates_data.get("date"):
                        context_lines.append(f"Billing Period: {rates_data['date']}")
                    for entry in rates_data["data"]:
                        context_lines.append(
                            f"- {entry['kwh']} kWh bracket: ₱{entry['rate']:.4f}/kWh (Generation: ₱{entry['generation_rate']:.4f}/kWh)"
                        )
        except Exception:
            pass

    appliance_db_path = os.path.join(os.path.dirname(__file__), "appliance_db.json")
    if os.path.exists(appliance_db_path):
        try:
            with open(appliance_db_path, "r", encoding="utf-8") as f:
                app_data = json.load(f)
                appliances = app_data.get("appliances", [])
                if appliances:
                    context_lines.append("\n--- Appliance Wattages Database (Sample) ---")
                    for app in appliances[:15]:
                        context_lines.append(
                            f"- {app['name']}: ~{app['average_wattage']}W ({app['wattage_range']}) - {app.get('description', '')}"
                        )
        except Exception:
            pass

    return "\n".join(context_lines)

async def ask_gemini_chatbot(user_query: str) -> str:
    """Send prompt to Gemini AI and return formatted response."""
    if not gemini_model:
        return (
            "⚠️ **AI Assistant Offline**\n"
            "The AI Chatbot requires `GEMINI_API_KEY` to be set in your `.env` or Render environment variables."
        )

    system_context = get_system_context()
    full_prompt = f"{system_context}\n\nUser Question: {user_query}\n\nAnswer:"

    try:
        response = await asyncio.to_thread(gemini_model.generate_content, full_prompt)
        text = response.text.strip()
        if len(text) > 1900:
            text = text[:1900] + "...\n*(response truncated due to Discord length limit)*"
        return text
    except Exception as e:
        logger.error(f"Gemini API query error: {e}")
        return f"❌ Error contacting AI Assistant: `{str(e)}`"

# Channel limitation check
def is_allowed_channel():

    def predicate(interaction: discord.Interaction) -> bool:
        if not ALLOWED_CHANNEL_ID:
            return True  # If not set, allow all channels
        try:
            return interaction.channel_id == int(ALLOWED_CHANNEL_ID)
        except (ValueError, TypeError):
            return True
    return app_commands.check(predicate)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            f"❌ This bot can only be used in the dedicated channel: <#{ALLOWED_CHANNEL_ID}>.",
            ephemeral=True
        )
    else:
        logger.error(f"Command error: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred while running the command.", ephemeral=True)
            elif interaction.followup:
                await interaction.followup.send("❌ An error occurred while running the command.", ephemeral=True)
        except Exception:
            pass

# File paths
APPLIANCE_DB_PATH = os.path.join(os.path.dirname(__file__), "appliance_db.json")
PREFERENCES_PATH = os.path.join(os.path.dirname(__file__), "user_preferences.json")

# Colors
MERALCO_ORANGE = discord.Color.from_rgb(252, 136, 3)
GREEN = discord.Color.green()
RED = discord.Color.red()
GREY = discord.Color.light_grey()

# Helper Functions
def load_appliance_db() -> dict:
    if os.path.exists(APPLIANCE_DB_PATH):
        try:
            with open(APPLIANCE_DB_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading appliance database: {e}")
    return {"appliances": []}

def get_user_bracket(user_id: int) -> int:
    """Get user's customized kWh bracket (default is 200 kWh)."""
    if os.path.exists(PREFERENCES_PATH):
        try:
            with open(PREFERENCES_PATH, "r") as f:
                prefs = json.load(f)
                return prefs.get(str(user_id), 200)
        except Exception as e:
            logger.error(f"Error reading user preferences: {e}")
    return 200

def save_user_bracket(user_id: int, bracket: int) -> None:
    prefs = {}
    if os.path.exists(PREFERENCES_PATH):
        try:
            with open(PREFERENCES_PATH, "r") as f:
                prefs = json.load(f)
        except Exception:
            pass
    prefs[str(user_id)] = bracket
    try:
        with open(PREFERENCES_PATH, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving user preferences: {e}")

def get_rate_for_bracket(bracket: int) -> tuple[int, float, dict]:
    """Finds the rate entry corresponding to or closest to the given bracket."""
    try:
        # Check if rates.json exists, if not generate it
        if not os.path.exists(RATES_JSON_PATH):
            logger.info("rates.json not found, fetching rates online...")
            get_meralco_rates()
            
        with open(RATES_JSON_PATH, "r") as f:
            rates_data = json.load(f)
            
        if not rates_data.get("success") or not rates_data.get("data"):
            return 200, 11.50, {}
            
        entries = rates_data["data"]
        # Find the rate entry closest to the user's consumption bracket
        closest_entry = min(entries, key=lambda x: abs(x["kwh"] - bracket))
        return closest_entry["kwh"], closest_entry["rate"], closest_entry
    except Exception as e:
        logger.error(f"Error reading rate for bracket: {e}")
        return 200, 11.50, {}

# Default Rate Constants based on May 2026 Meralco rate components
DEFAULT_RATES = {
    "transmission": 1.4074,       # Transmission Charge (PHP/kWh)
    "systemLoss": 0.7994,         # System Loss Charge (PHP/kWh)
    
    # Progressive Distribution Tiers (PHP/kWh)
    "distTier1": 0.9803,          # 0 - 200 kWh
    "distTier2": 1.2908,          # 201 - 300 kWh
    "distTier3": 1.5837,          # 301 - 400 kWh
    "distTier4": 2.0941,          # 401+ kWh
    
    # Metering & Supply Fees
    "meteringFixed": 5.0000,      # Fixed Metering Charge (PHP/month)
    "meteringPerKwh": 0.3350,     # Metering Charge per kWh (PHP/kWh)
    "supplyFixed": 16.3800,       # Fixed Supply Charge (PHP/month)
    "supplyPerKwh": 0.4979,       # Supply Charge per kWh (PHP/kWh)
    
    "awatRefund": -0.4278,        # AWAT Refund/Collect (PHP/kWh)
    "regReset": -0.0023,          # Reg Reset Charge (PHP/kWh)
    
    # Component VAT Rates
    "vatGen": 0.0941,             # Generation VAT
    "vatTrans": 0.1126,           # Transmission VAT
    "vatSysLoss": 0.0966,         # System Loss VAT
    "vatOthers": 0.1200,          # Distribution/Other VAT
    
    # Other Government Taxes
    "rptRate": 0.0062,            # Real Property Tax (PHP/kWh)
    "lftRate": 0.0050,            # Local Franchise Tax Rate
    
    # Non-VAT Subsidies
    "universalRate": 0.3216,      # Total Universal Charges (PHP/kWh)
    "fitAll": 0.2011,             # FIT-All (PHP/kWh)
    "lifelineRate": 0.0100,       # Lifeline Subsidy (PHP/kWh)
    "seniorRate": 0.0001          # Senior Citizen Subsidy (PHP/kWh)
}

def round_to_2_dec(val: float) -> float:
    """Matches JavaScript's Math.round(val * 100) / 100 behavior for rounding."""
    if val >= 0:
        return int(val * 100 + 0.5) / 100.0
    else:
        return int(val * 100 - 0.5) / 100.0

def calculate_bill_breakdown(kwh: float, gen_rate: float, other_charges: float = 0.0) -> dict:
    """Calculates Meralco bill breakdown matching the index.html logic."""
    gen_cost = round_to_2_dec(kwh * gen_rate)
    trans_cost = round_to_2_dec(kwh * DEFAULT_RATES["transmission"])
    sys_loss_cost = round_to_2_dec(kwh * DEFAULT_RATES["systemLoss"])
    
    # Progressive distribution rate based on tiers
    if kwh <= 200:
        dist_rate = DEFAULT_RATES["distTier1"]
    elif kwh <= 300:
        dist_rate = DEFAULT_RATES["distTier2"]
    elif kwh <= 400:
        dist_rate = DEFAULT_RATES["distTier3"]
    else:
        dist_rate = DEFAULT_RATES["distTier4"]
        
    dist_cost = round_to_2_dec(kwh * dist_rate)
    
    metering_cost = round_to_2_dec(kwh * DEFAULT_RATES["meteringPerKwh"]) + DEFAULT_RATES["meteringFixed"]
    supply_cost = round_to_2_dec(kwh * DEFAULT_RATES["supplyPerKwh"]) + DEFAULT_RATES["supplyFixed"]
    
    awat_refund = round_to_2_dec(kwh * DEFAULT_RATES["awatRefund"])
    reg_reset = round_to_2_dec(kwh * DEFAULT_RATES["regReset"])
    
    senior_cost = round_to_2_dec(kwh * DEFAULT_RATES["seniorRate"])
    
    # VAT Calculations per component
    gen_vat = round_to_2_dec(gen_cost * DEFAULT_RATES["vatGen"])
    trans_vat = round_to_2_dec(trans_cost * DEFAULT_RATES["vatTrans"])
    sys_loss_vat = round_to_2_dec(sys_loss_cost * DEFAULT_RATES["vatSysLoss"])
    
    dist_meralco_total = dist_cost + metering_cost + supply_cost + awat_refund + reg_reset
    dist_vat = round_to_2_dec(dist_meralco_total * DEFAULT_RATES["vatOthers"])
    senior_vat = round_to_2_dec(senior_cost * DEFAULT_RATES["vatOthers"])
    
    total_vat = gen_vat + trans_vat + sys_loss_vat + dist_vat + senior_vat
    
    # Government Taxes (RPT, LFT)
    rpt_cost = round_to_2_dec(kwh * DEFAULT_RATES["rptRate"])
    lft_base = gen_cost + trans_cost + sys_loss_cost + dist_meralco_total + senior_cost + rpt_cost
    lft_cost = round_to_2_dec(lft_base * DEFAULT_RATES["lftRate"])
    
    gov_taxes_total = rpt_cost + lft_cost + total_vat
    
    # Non-VAT Subsidies & Universal Charges
    uc_npcspug = round_to_2_dec(kwh * DEFAULT_RATES["universalRate"] * (0.2662 / 0.3216))
    uc_redci = round_to_2_dec(kwh * DEFAULT_RATES["universalRate"] * (0.0101 / 0.3216))
    uc_env = round_to_2_dec(kwh * DEFAULT_RATES["universalRate"] * (0.0025 / 0.3216))
    uc_stranded = round_to_2_dec(kwh * DEFAULT_RATES["universalRate"] * (0.0428 / 0.3216))
    universal_charges_total = uc_npcspug + uc_redci + uc_env + uc_stranded
    
    fit_all_cost = round_to_2_dec(kwh * DEFAULT_RATES["fitAll"])
    lifeline_cost = round_to_2_dec(kwh * DEFAULT_RATES["lifelineRate"])
    
    non_vat_subsidies_total = universal_charges_total + fit_all_cost + lifeline_cost
    
    energy_amount = gen_cost + trans_cost + sys_loss_cost + dist_meralco_total + senior_cost + gov_taxes_total + non_vat_subsidies_total
    total_bill = energy_amount + other_charges
    
    return {
        "gen_cost": gen_cost,
        "trans_cost": trans_cost,
        "sys_loss_cost": sys_loss_cost,
        "dist_cost": dist_cost,
        "metering_cost": metering_cost,
        "supply_cost": supply_cost,
        "awat_refund": awat_refund,
        "reg_reset": reg_reset,
        "dist_meralco_total": dist_meralco_total,
        "senior_cost": senior_cost,
        "total_vat": total_vat,
        "rpt_cost": rpt_cost,
        "lft_cost": lft_cost,
        "gov_taxes_total": gov_taxes_total,
        "universal_charges_total": universal_charges_total,
        "fit_all_cost": fit_all_cost,
        "lifeline_cost": lifeline_cost,
        "non_vat_subsidies_total": non_vat_subsidies_total,
        "energy_amount": energy_amount,
        "total_bill": total_bill,
        "dist_rate": dist_rate
    }


# Autocomplete function for appliances in /calculate and /wattage
async def appliance_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    db = load_appliance_db()
    choices = []
    for app in db.get("appliances", []):
        if current.lower() in app["name"].lower():
            # Limit the name length for choices to 100 characters (discord limit)
            choices.append(app_commands.Choice(name=app["name"][:100], value=app["name"]))
    return choices[:25]  # Limit to 25 choices max

# Bot Events
@bot.event
async def on_ready():
    logger.info(f"Bot connected as {bot.user} (ID: {bot.user.id})")
    
    # Initialize rates on boot if not already present
    if not os.path.exists(RATES_JSON_PATH):
        logger.info("Initializing rates.json on startup...")
        get_meralco_rates()
        
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands globally.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

# Bot Slash Commands

@bot.tree.command(name="rates", description="View the latest parsed Meralco residential rates.")
@is_allowed_channel()
async def rates(interaction: discord.Interaction):
    await interaction.response.defer()
    
    # Get rates
    rates_info = get_meralco_rates()
    if not rates_info.get("success"):
        await interaction.followup.send("❌ Error fetching rates. Please try again later.")
        return

    billing_date = rates_info.get("date", "N/A")
    data_list = rates_info.get("data", [])
    source_url = rates_info.get("meta", {}).get("source", "https://www.meralco.com.ph")

    embed = discord.Embed(
        title=f"⚡ Meralco Residential Rates ({billing_date})",
        description="Showing electricity rates per consumption bracket (VAT-exclusive).",
        url=source_url,
        color=MERALCO_ORANGE
    )

    # Let's add the 200 kWh (typical household) rate as the main highlight
    typical_entry = next((item for item in data_list if item["kwh"] == 200), None)
    if not typical_entry and data_list:
        typical_entry = data_list[min(range(len(data_list)), key=lambda i: abs(data_list[i]["kwh"] - 200))]

    if typical_entry:
        trend_emoji = "📈" if typical_entry["trend"] == "up" else "📉" if typical_entry["trend"] == "down" else "➡️"
        change_text = f"{typical_entry['rate_change']:+g} PHP/kWh ({typical_entry['rate_change_percent']}% change)" if typical_entry['rate_change'] else "No change"
        
        embed.add_field(
            name="💡 Typical Household (200 kWh)",
            value=f"**Rate:** `₱{typical_entry['rate']:.4f}` per kWh\n**MoM Trend:** {trend_emoji} {change_text}",
            inline=False
        )

    # Add other common brackets (50, 100, 300, 500)
    brackets_to_show = [50, 100, 300, 500]
    other_brackets_text = []
    for entry in data_list:
        if entry["kwh"] in brackets_to_show:
            trend_icon = "🔺" if entry["trend"] == "up" else "🔻" if entry["trend"] == "down" else "🔸"
            other_brackets_text.append(f"• **{entry['kwh']} kWh:** `₱{entry['rate']:.4f}/kWh` ({trend_icon})")
    
    if other_brackets_text:
        embed.add_field(
            name="📊 Other Consumption Brackets",
            value="\n".join(other_brackets_text),
            inline=False
        )

    embed.set_footer(text="Rates parsed from official Meralco bulletins. Estimates only.")
    
    if rates_info.get("warning"):
        embed.add_field(name="⚠️ Note", value=rates_info["warning"], inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="bracket", description="Set your household's monthly consumption bracket (e.g. 200 kWh).")
@app_commands.describe(kwh="Your typical monthly electricity consumption in kWh (e.g. 200, 300, 500)")
@is_allowed_channel()
async def bracket(interaction: discord.Interaction, kwh: int):
    if kwh <= 0:
        await interaction.response.send_message("❌ Please enter a positive value greater than 0.", ephemeral=True)
        return

    save_user_bracket(interaction.user.id, kwh)
    
    # Get the closest rate bracket to confirm
    matched_kwh, rate, _ = get_rate_for_bracket(kwh)
    
    embed = discord.Embed(
        title="🔧 Preferences Saved",
        description=f"Your default consumption bracket has been set to **{kwh} kWh**.",
        color=GREEN
    )
    embed.add_field(
        name="Rate Applied",
        value=f"Calculations will now use the closest bracket: **{matched_kwh} kWh** rate (`₱{rate:.4f}/kWh`)."
    )
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="calculate", description="Estimate the cost of running an appliance.")
@app_commands.describe(
    appliance="Select an appliance or type custom wattage (e.g. '50' or '50W')",
    hours_per_day="How many hours is the appliance used per day?",
    days="Number of days (default is 30)"
)
@app_commands.autocomplete(appliance=appliance_autocomplete)
@is_allowed_channel()
async def calculate(interaction: discord.Interaction, appliance: str, hours_per_day: float, days: int = 30):
    await interaction.response.defer()

    if hours_per_day <= 0 or hours_per_day > 24:
        await interaction.followup.send("❌ Hours per day must be between 0 and 24.")
        return
    if days <= 0 or days > 365:
        await interaction.followup.send("❌ Days must be between 1 and 365.")
        return

    db = load_appliance_db()
    matched_app = None
    wattage = None

    # Check if the input is a raw number (e.g. "150") or has a 'W' (e.g. "150W")
    clean_input = appliance.strip().upper()
    wattage_match = re.match(r"^(\d+)\s*W?$", clean_input)

    if wattage_match:
        wattage = int(wattage_match.group(1))
        display_name = f"Custom Appliance ({wattage}W)"
    else:
        # Search the appliance database
        for app in db.get("appliances", []):
            if app["name"].lower() == appliance.lower():
                matched_app = app
                wattage = app["average_wattage"]
                display_name = app["name"]
                break
        
        # Fallback if no exact match but matching name
        if not matched_app:
            for app in db.get("appliances", []):
                if appliance.lower() in app["name"].lower():
                    matched_app = app
                    wattage = app["average_wattage"]
                    display_name = app["name"]
                    break

    if wattage is None:
        await interaction.followup.send(
            f"❌ Could not find appliance '{appliance}' in the database.\n"
            f"Please select from the dropdown options or enter a direct wattage like `150` or `150W`."
        )
        return

    # Calculate consumption
    # kWh = (Wattage * Hours * Days) / 1000
    daily_kwh = (wattage * hours_per_day) / 1000
    total_kwh = daily_kwh * days

    # Get rate for user bracket
    user_bracket = get_user_bracket(interaction.user.id)
    matched_kwh, rate, rate_entry = get_rate_for_bracket(user_bracket)

    daily_cost = daily_kwh * rate
    total_cost = total_kwh * rate

    embed = discord.Embed(
        title="⚡ Appliance Cost Estimate",
        description=f"Cost estimate using the **{matched_kwh} kWh** rate (`₱{rate:.4f}/kWh`).",
        color=MERALCO_ORANGE
    )
    embed.add_field(name="🔌 Appliance", value=display_name, inline=True)
    embed.add_field(name="🔋 Wattage", value=f"{wattage} Watts", inline=True)
    embed.add_field(name="⏱️ Usage", value=f"{hours_per_day} hrs/day for {days} days", inline=True)
    
    embed.add_field(
        name="📈 Consumption",
        value=f"Daily: `{daily_kwh:.3f} kWh`\nTotal: `{total_kwh:.3f} kWh`",
        inline=True
    )
    embed.add_field(
        name="💰 Estimated Cost",
        value=f"Daily: **₱{daily_cost:.2f}**\nTotal ({days} days): **₱{total_cost:.2f}**",
        inline=True
    )

    if matched_app and matched_app.get("description"):
        embed.add_field(name="ℹ️ Details", value=matched_app["description"], inline=False)

    embed.set_footer(
        text=f"Calculated for bracket: {user_bracket} kWh (User Setting). Use /bracket to change."
    )

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="appliances", description="List typical Philippine household appliances and categories.")
@is_allowed_channel()
async def appliances(interaction: discord.Interaction):
    db = load_appliance_db()
    if not db.get("appliances"):
        await interaction.response.send_message("❌ Appliance database is empty.", ephemeral=True)
        return

    # Group appliances by category
    categories = {}
    for app in db["appliances"]:
        cat = app["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(f"• {app['name']} (`{app['average_wattage']}W`)")

    embed = discord.Embed(
        title="🏠 Typical Philippine Household Appliance Wattages",
        description="Use these average wattages in your calculations. Type `/wattage <appliance>` to search detailed ranges.",
        color=MERALCO_ORANGE
    )

    for cat, items in categories.items():
        embed.add_field(
            name=f"📂 {cat}",
            value="\n".join(items[:8]), # limit display to first 8 to avoid hitting embed field limits
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="wattage", description="Search for typical wattage of an appliance.")
@app_commands.describe(query="The appliance to search for (e.g. 'AC', 'refrigerator', 'fan')")
@app_commands.autocomplete(query=appliance_autocomplete)
@is_allowed_channel()
async def wattage(interaction: discord.Interaction, query: str):
    db = load_appliance_db()
    results = []

    # Search query in database names and descriptions
    for app in db.get("appliances", []):
        if query.lower() in app["name"].lower() or query.lower() in app["category"].lower():
            results.append(app)

    if not results:
        await interaction.response.send_message(
            f"❌ No matching appliances found for '{query}'.\n"
            f"Try searching simpler keywords like 'fan', 'AC', 'ref', 'TV'.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"🔍 Wattage Search Results: '{query}'",
        color=MERALCO_ORANGE
    )

    # Show up to 5 results to keep embed clean
    for app in results[:5]:
        embed.add_field(
            name=f"🔌 {app['name']}",
            value=f"**Average Wattage:** `{app['average_wattage']} W`\n"
                  f"**Range:** {app['wattage_range']}\n"
                  f"**Category:** {app['category']}\n"
                  f"*Description:* {app['description']}",
            inline=False
        )

    if len(results) > 5:
        embed.set_footer(text=f"Showing 5 of {len(results)} matches.")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="update_rates", description="Force check and update the Meralco rate PDF cache.")
@is_allowed_channel()
async def update_rates(interaction: discord.Interaction):
    # Restrict command to admin or specific users if needed.
    # Here we allow simple execution and display status.
    await interaction.response.defer()
    
    rates_info = get_meralco_rates()
    if rates_info.get("success"):
        embed = discord.Embed(
            title="✅ Rates Updated Successfully",
            description=f"Current Rate Schedule Date: **{rates_info.get('date')}**",
            color=GREEN
        )
        embed.add_field(name="Source PDF", value=rates_info.get("meta", {}).get("source", "N/A"))
        if rates_info.get("warning"):
            embed.add_field(name="Warning/Fallback Note", value=rates_info["warning"])
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(f"❌ Failed to update rates: {rates_info.get('error')}")


async def calculate_generation_charge(interaction: discord.Interaction, kwh: float):
    if kwh <= 0:
        await interaction.response.send_message("❌ Please enter a positive kWh value greater than 0.", ephemeral=True)
        return

    await interaction.response.defer()

    # Get rates info
    rates_info = get_meralco_rates()
    if not rates_info.get("success"):
        await interaction.followup.send("❌ Error fetching rates. Please try again later.")
        return

    # Extract billing date
    billing_date = rates_info.get("date")
    if not billing_date:
        source_url = rates_info.get("meta", {}).get("source", "")
        match = re.search(r"(\d{2})-(\d{4})", source_url)
        if match:
            month_num = int(match.group(1))
            year = match.group(2)
            month_name = calendar.month_name[month_num]
            billing_date = f"{month_name} {year}"
        else:
            billing_date = "N/A"

    # Find closest rate bracket
    matched_kwh, total_rate, rate_entry = get_rate_for_bracket(int(kwh))
    generation_rate = rate_entry.get("generation_rate", 8.7942)

    # Calculate generation charge amount
    generation_amount = kwh * generation_rate

    embed = discord.Embed(
        title="⚡ Household Base Generation Charge",
        description=f"Estimated Generation Charge component for the billing period of **{billing_date}**.",
        color=MERALCO_ORANGE
    )
    embed.add_field(name="📊 Consumption", value=f"`{kwh:,.2f} kWh`", inline=True)
    embed.add_field(name="💡 Generation Rate", value=f"`₱{generation_rate:.4f} / kWh`", inline=True)
    embed.add_field(name="💰 Generation Charge Amount", value=f"**₱{generation_amount:,.2f}**", inline=False)
    
    embed.set_footer(
        text=f"Calculated for bracket: {matched_kwh} kWh. Base price only. VAT and other charges not included."
    )

    if rates_info.get("warning"):
        embed.add_field(name="⚠️ Note", value=rates_info["warning"], inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="generation_charge", description="Calculate the household monthly electricity generation charge based on kWh consumption.")
@app_commands.describe(kwh="Your monthly electricity consumption in kWh (e.g. 413, 200, 300)")
@is_allowed_channel()
async def generation_charge(interaction: discord.Interaction, kwh: float):
    await calculate_generation_charge(interaction, kwh)


@bot.tree.command(name="base_generation", description="Calculate the household base generation price based on kWh consumption.")
@app_commands.describe(kwh="Your monthly electricity consumption in kWh (e.g. 413, 200, 300)")
@is_allowed_channel()
async def base_generation(interaction: discord.Interaction, kwh: float):
    await calculate_generation_charge(interaction, kwh)


@bot.tree.command(name="total_bill", description="Calculate the household monthly electricity total bill based on kWh consumption.")
@app_commands.describe(
    kwh="Your monthly electricity consumption in kWh (e.g. 413, 200, 300)",
    generation_rate="Optional custom generation rate in PHP/kWh. If omitted, uses the latest rate from closest bracket.",
    other_charges="Optional extra charges/deposits in PHP (default 0.0)"
)
@is_allowed_channel()
async def total_bill(
    interaction: discord.Interaction, 
    kwh: float, 
    generation_rate: Optional[float] = None, 
    other_charges: float = 0.0
):
    if kwh <= 0:
        await interaction.response.send_message("❌ Please enter a positive kWh value greater than 0.", ephemeral=True)
        return

    await interaction.response.defer()

    # Get latest rates info for metadata/billing date
    rates_info = get_meralco_rates()
    if not rates_info.get("success"):
        await interaction.followup.send("❌ Error fetching rates. Please try again later.")
        return

    # Extract billing date
    billing_date = rates_info.get("date")
    if not billing_date:
        source_url = rates_info.get("meta", {}).get("source", "")
        match = re.search(r"(\d{2})-(\d{4})", source_url)
        if match:
            month_num = int(match.group(1))
            year = match.group(2)
            month_name = calendar.month_name[month_num]
            billing_date = f"{month_name} {year}"
        else:
            billing_date = "N/A"

    # Find closest rate bracket to fetch generation rate if not provided
    matched_kwh, total_rate, rate_entry = get_rate_for_bracket(int(kwh))
    if generation_rate is None:
        generation_rate = rate_entry.get("generation_rate", 8.7942)

    # Calculate breakdown
    breakdown = calculate_bill_breakdown(kwh, generation_rate, other_charges)

    embed = discord.Embed(
        title="⚡ Meralco Estimated Total Bill",
        description=f"Estimated electricity bill breakdown for **{kwh:,.2f} kWh** for the billing period of **{billing_date}**.",
        color=MERALCO_ORANGE
    )

    embed.add_field(
        name="💰 Total Amount Due",
        value=f"**₱{breakdown['total_bill']:,.2f}**",
        inline=False
    )

    embed.add_field(
        name="🔌 Billing Parameters",
        value=f"• **Consumption:** `{kwh:,.2f} kWh`\n"
              f"• **Generation Rate:** `₱{generation_rate:.4f}/kWh` (Bracket: {matched_kwh} kWh)\n"
              f"• **Other Charges:** `₱{other_charges:,.2f}`",
        inline=False
    )

    breakdown_text = (
        f"```yaml\n"
        f"Generation Charge:    ₱{breakdown['gen_cost']:8,.2f}\n"
        f"Transmission Charge:  ₱{breakdown['trans_cost']:8,.2f}\n"
        f"System Loss Charge:   ₱{breakdown['sys_loss_cost']:8,.2f}\n"
        f"Distribution Charge:  ₱{breakdown['dist_cost']:8,.2f} (rate: ₱{breakdown['dist_rate']:.4f}/kWh)\n"
        f"Metering & Supply:    ₱{breakdown['metering_cost'] + breakdown['supply_cost']:8,.2f}\n"
        f"AWAT Refund/Collect:  ₱{breakdown['awat_refund']:8,.2f}\n"
        f"Government Taxes/VAT: ₱{breakdown['gov_taxes_total']:8,.2f}\n"
        f"Subsidies & Non-VAT:  ₱{breakdown['non_vat_subsidies_total']:8,.2f}\n"
        f"Other Charges:        ₱{other_charges:8,.2f}\n"
        f"-------------------------------\n"
        f"Total Estimated Bill: ₱{breakdown['total_bill']:8,.2f}\n"
        f"```"
    )

    embed.add_field(
        name="📊 Charge Components Breakdown",
        value=breakdown_text,
        inline=False
    )

    embed.set_footer(
        text="Estimated cost only. Actual Meralco bill may vary depending on taxes, charges, and billing adjustments."
    )

    if rates_info.get("warning"):
        embed.add_field(name="⚠️ Note", value=rates_info["warning"], inline=False)

    await interaction.followup.send(embed=embed)


# ==============================================================================
# AI CHATBOT COMMAND & EVENT HANDLERS
# ==============================================================================

@bot.tree.command(name="ask", description="Ask BillShock AI anything about Meralco rates, appliances, or energy saving.")
@app_commands.describe(question="Your question about electricity, bills, appliances, or rates")
@is_allowed_channel()
async def ask_command(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    response_text = await ask_gemini_chatbot(question)

    embed = discord.Embed(
        title="🤖 BillShock AI Assistant",
        description=response_text,
        color=MERALCO_ORANGE
    )
    embed.set_footer(text="Ask me anything using /ask <question> or by @mentioning me!")
    await interaction.followup.send(embed=embed)


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages sent by bots
    if message.author.bot:
        return

    # Check if channel restriction applies
    if ALLOWED_CHANNEL_ID:
        try:
            if message.channel.id != int(ALLOWED_CHANNEL_ID):
                return
        except (ValueError, TypeError):
            pass

    # Check if the bot was mentioned in the message
    if bot.user in message.mentions:
        # Strip out the mention tag to get pure query text
        clean_text = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not clean_text:
            await message.reply("Hello! 👋 I'm **BillShock AI**. Ask me anything about Meralco rates, appliance costs, or energy saving tips!")
            return

        async with message.channel.typing():
            ai_reply = await ask_gemini_chatbot(clean_text)
            
        await message.reply(ai_reply)

    await bot.process_commands(message)





# Lightweight HTTP Health Server for Render Free Tier Web Service
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"BillShock Discord Bot is running!")

    def log_message(self, format, *args):
        pass  # Suppress web server logs from polluting bot logs

def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"Health check HTTP server started on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health server: {e}")

# Start Bot
if __name__ == "__main__":
    if not TOKEN:
        print("❌ Error: DISCORD_BOT_TOKEN environment variable not set in .env")
    else:
        # Start dummy HTTP health check server for Render Web Service compliance
        threading.Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)

