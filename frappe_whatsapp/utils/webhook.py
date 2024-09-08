"""Webhook."""
import frappe
import json
import requests
import time
from werkzeug.wrappers import Response
import frappe.utils
from openai import OpenAI
import google.generativeai as genai
import os


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()
	return post()


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	webhook_verify_token = frappe.db.get_single_value(
		"WhatsApp Settings", "webhook_verify_token"
	)

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def post():
	"""Post."""
	data = frappe.local.form_dict
	frappe.get_doc({
		"doctype": "WhatsApp Notification Log",
		"template": "Webhook",
		"meta_data": json.dumps(data)
	}).insert(ignore_permissions=True)

	messages = []
	try:
		messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
	except KeyError:
		messages = data["entry"]["changes"][0]["value"].get("messages", [])

	if messages:
		for message in messages:
			message_type = message['type']
			is_reply = True if message.get('context') else False
			reply_to_message_id = message['context']['id'] if is_reply else None
			if message_type == 'text':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['text']['body'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type":message_type
				}).insert(ignore_permissions=True)
				reply_message(message['from'], message['text']['body'])
			elif message_type == 'reaction':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['reaction']['emoji'],
					"reply_to_message_id": message['reaction']['message_id'],
					"message_id": message['id'],
					"content_type": "reaction"
				}).insert(ignore_permissions=True)
			elif message_type == 'interactive':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['interactive']['nfm_reply']['response_json'],
					"message_id": message['id'],
					"content_type": "flow"
				}).insert(ignore_permissions=True)
			elif message_type in ["image", "audio", "video", "document"]:
				settings = frappe.get_doc(
							"WhatsApp Settings", "WhatsApp Settings",
						)
				token = settings.get_password("token")
				url = f"{settings.url}/{settings.version}/"


				media_id = message[message_type]["id"]
				headers = {
					'Authorization': 'Bearer ' + token

				}
				response = requests.get(f'{url}{media_id}/', headers=headers)

				if response.status_code == 200:
					media_data = response.json()
					media_url = media_data.get("url")
					mime_type = media_data.get("mime_type")
					file_extension = mime_type.split('/')[1]

					media_response = requests.get(media_url, headers=headers)
					if media_response.status_code == 200:

						file_data = media_response.content
						file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

						message_doc = frappe.get_doc({
							"doctype": "WhatsApp Message",
							"type": "Incoming",
							"from": message['from'],
							"message_id": message['id'],
							"reply_to_message_id": reply_to_message_id,
							"is_reply": is_reply,
							"message": message[message_type].get("caption",f"/files/{file_name}"),
							"content_type" : message_type
						}).insert(ignore_permissions=True)

						file = frappe.get_doc(
							{
								"doctype": "File",
								"file_name": file_name,
								"attached_to_doctype": "WhatsApp Message",
								"attached_to_name": message_doc.name,
								"content": file_data,
								"attached_to_field": "attach"
							}
						).save(ignore_permissions=True)


						message_doc.attach = file.file_url
						message_doc.save()
			else:
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message_id": message['id'],
					"message": message[message_type].get(message_type),
					"content_type" : message_type
				}).insert(ignore_permissions=True)

	else:
		changes = None
		try:
			changes = data["entry"][0]["changes"][0]
		except KeyError:
			changes = data["entry"]["changes"][0]
		update_status(changes)
	return

def update_status(data):
	"""Update status hook."""
	if data.get("field") == "message_template_status_update":
		update_template_status(data['value'])

	elif data.get("field") == "messages":
		update_message_status(data['value'])

def update_template_status(data):
	"""Update template status."""
	frappe.db.sql(
		"""UPDATE `tabWhatsApp Templates`
		SET status = %(event)s
		WHERE id = %(message_template_id)s""",
		data
	)

def update_message_status(data):
	"""Update message status."""
	id = data['statuses'][0]['id']
	status = data['statuses'][0]['status']
	conversation = data['statuses'][0].get('conversation', {}).get('id')
	name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

	doc = frappe.get_doc("WhatsApp Message", name)
	doc.status = status
	if conversation:
		doc.conversation_id = conversation
	doc.save(ignore_permissions=True)

def reply_message(phone_number, message):
	ai_response = get_response_from_gemini(phone_number, message)
	frappe.get_doc({
					"doctype": "WhatsApp Message",
					"label":"ChatBot",
					"type": "Outgoing",
					"to":phone_number,
					"message":ai_response,
					"message_type":"Manual",
					"content_type":"text"
				}).insert(ignore_permissions=True)

def get_prompt():
	prompt_file_path = os.path.join(os.path.dirname(__file__), 'prompt.txt')
	with open(prompt_file_path, 'r') as file:
		return file.read().strip()

def get_chathistory(phone_number):
    try:
        msg = frappe.db.sql("""
            SELECT message, type
            FROM `tabWhatsApp Message`
            WHERE `from` = %s OR `to` = %s
        """, (phone_number, phone_number), as_dict=True)

        chat_history = []
        
        prompt_added = False

        for ele in msg:
            if ele.type == "Incoming":
                if not prompt_added:
                    chat_history.append({"role": "user", "parts": get_prompt() + ele.message})
                    prompt_added = True
                else:
                    chat_history.append({"role": "user", "parts": ele.message})
            else:
                chat_history.append({"role": "model", "parts": ele.message})

        return chat_history

    except Exception as e:
        return []


# def get_chathistory(phone_number):
# 	try:
# 		msg = frappe.db.sql("""
# 			SELECT message, type
# 			FROM `tabWhatsApp Message`
# 			WHERE `from` = %s OR `to` = %s
# 		""", (phone_number, phone_number), as_dict=True)

# 		# Initializing chat history as an empty string
# 		chat_history = ""

# 		# Iterating over the results and appending messages to the chat history
# 		for ele in msg:
# 			if ele.type == "Incoming":
# 				chat_history = chat_history+ " user: " + ele.message +"\n"
# 			else:
# 				chat_history = chat_history+ " assitant: " + ele.message +"\n"

# 		return chat_history

# 	except Exception as e:
# 		return 

def get_response_from_gpt(phone_number,message):
	try:
		settings = frappe.get_doc(
				"WhatsApp Settings",
				"WhatsApp Settings",
			)
		open_ai_api_key = settings.get_password("openai_api_key")
		client = OpenAI(api_key=open_ai_api_key)
		chat_history = get_chathistory(phone_number)
		prompt = get_prompt()

		response = client.chat.completions.create(
			model="gpt-3.5-turbo",
			messages=[
				{"role": "system", "content": prompt + chat_history},
				{"role": "user", "content": message},
			],
			temperature=0,
		)
		return response.choices[0].message.content
	except Exception as e:
		frappe.logger().debug({"open ai": e})
		return "Currently unavailable. Please try again later."

def get_response_from_gemini(phone_number, message):
	try:
		settings = frappe.get_doc(
				"WhatsApp Settings",
				"WhatsApp Settings",
			)
		genai_api_key = settings.get_password("gemini_api_key")
		genai.configure(api_key=genai_api_key)
		generation_config = {
		"temperature": 0,
		"response_mime_type": "text/plain",
		}
		model = genai.GenerativeModel(
		model_name="gemini-1.5-pro-latest",
		generation_config=generation_config,
		)
		chat = model.start_chat(history=get_chathistory(phone_number))
		response = chat.send_message(message)
		return response.text
	except Exception as e:
		frappe.logger().debug({"gemini": e})
		return "Currently unavailable. Please try again later."
