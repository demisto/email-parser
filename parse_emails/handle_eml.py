from __future__ import print_function

import base64
import email
import email.utils
import logging
import os
import quopri
import re
import tempfile
from base64 import b64decode
from email import message_from_string
from email.parser import HeaderParser
from email.utils import getaddresses

from parse_emails.common import convert_to_unicode
from parse_emails.handle_msg import handle_msg

MIME_ENCODED_WORD = re.compile(r'(.*)=\?(.+)\?([B|Q])\?(.+)\?=(.*)')  # guardrails-disable-line
ENCODINGS_TYPES = {'utf-8', 'iso8859-1'}


def handle_eml(file_path, b64=False, file_name=None, parse_only_headers=False, max_depth=3, bom=False, original_depth=3):
    global ENCODINGS_TYPES

    if max_depth == 0:
        return None, []

    with open(file_path, 'rb') as emlFile:
        handle_SMTP_headers(emlFile)
        file_data = emlFile.read()
        if b64:
            file_data = b64decode(file_data)
        if bom:
            # decode bytes taking into account BOM and re-encode to utf-8
            file_data = file_data.decode("utf-8-sig")

        if isinstance(file_data, bytes):
            file_data = file_data.decode('utf-8', 'ignore')

        parser = HeaderParser()
        headers = parser.parsestr(file_data)

        # headers is a Message object implementing magic methods of set/get item and contains.
        # message object 'contains' method transforms its keys to lower-case, hence there is not a difference when
        # approaching it with any casing type, for example, 'message-id' or 'Message-ID' or 'Message-id' or
        # 'MeSSage_Id' are all searching for the same key in the headers object.
        if "message-id" in headers:
            message_id_content = headers["message-id"]
            del headers["message-id"]
            headers["Message-ID"] = message_id_content

        header_list = []
        headers_map = {}  # type: dict
        for item in headers.items():
            val = unfold(item[1])
            value = convert_to_unicode(val)
            item_dict = {
                "name": item[0],
                "value": value
            }

            # old way to map headers
            header_list.append(item_dict)

            # new way to map headers - dictionary
            if item[0] in headers_map:
                # in case there is already such header
                # then add that header value to value array
                if not isinstance(headers_map[item[0]], list):
                    # convert the existing value to array
                    headers_map[item[0]] = [headers_map[item[0]]]

                # add the new value to the value array
                headers_map[item[0]].append(value)
            else:
                headers_map[item[0]] = value

        eml = message_from_string(file_data)
        if not eml:
            raise Exception("Could not parse eml file!")

        if parse_only_headers:
            return {"HeadersMap": headers_map}, []

        html = ''
        text = ''
        attachment_names = []
        attachment_content_ids = []
        attachment_content_dispositions = []
        attachment_content = []
        attachments_images = []  # could be .png / jpg files.

        attached_emails = []
        parts = [eml]

        while parts:
            part = parts.pop()
            if (part.is_multipart() or part.get_content_type().startswith('multipart')) \
                    and "attachment" not in part.get("Content-Disposition", ""):
                parts += [part_ for part_ in part.get_payload() if isinstance(part_, email.message.Message)]

            elif part.get_filename() or "attachment" in part.get("Content-Disposition", ""):

                attachment_content_id = part.get('Content-ID')
                attachment_content_disposition = part.get('Content-Disposition')
                attachment_file_name = get_attachment_filename(part)

                if attachment_file_name is None and part.get('filename'):
                    attachment_file_name = os.path.normpath(part.get('filename'))
                    if os.path.isabs(attachment_file_name):
                        attachment_file_name = os.path.basename(attachment_file_name)

                if "message/rfc822" in part.get("Content-Type", "") \
                        or ("application/octet-stream" in part.get("Content-Type", "") and
                            attachment_file_name.endswith(".eml")):

                    # .eml files
                    file_content = ""  # type: str
                    base64_encoded = "base64" in part.get("Content-Transfer-Encoding", "")

                    if isinstance(part.get_payload(), list) and len(part.get_payload()) > 0:
                        if attachment_file_name is None or attachment_file_name == "" or attachment_file_name == 'None':
                            # in case there is no filename for the eml
                            # we will try to use mail subject as file name
                            # Subject will be in the email headers
                            attachment_name = part.get_payload()[0].get('Subject', "no_name_mail_attachment")
                            attachment_file_name = f'{attachment_name}.eml'

                        file_content = part.get_payload()[0].as_string().strip()
                        if base64_encoded:
                            try:
                                file_content = b64decode(file_content)

                            except TypeError:
                                pass  # In case the file is a string, decode=True for get_payload is not working

                    elif isinstance(part.get_payload(), str):
                        file_content = part.get_payload(decode=True)
                    else:
                        logging.debug("found eml attachment with Content-Type=message/rfc822 but has no payload")

                    if file_content:
                        # save the eml to war room as file entry
                        attachment_content.append(file_content)

                    if file_content and max_depth - 1 > 0:
                        f = tempfile.NamedTemporaryFile(delete=False)
                        try:
                            if isinstance(file_content, str):
                                file_content = file_content.encode('utf-8')
                            f.write(file_content)
                            f.close()
                            inner_eml, inner_attached_emails = handle_eml(file_path=f.name,
                                                                          file_name=attachment_file_name,
                                                                          max_depth=max_depth - 1,
                                                                          original_depth=original_depth)
                            if inner_eml:
                                inner_eml['ParentFileName'] = file_name
                            attached_emails.append(inner_eml)
                            attached_emails.extend(inner_attached_emails)

                        finally:
                            os.remove(f.name)
                    if not file_content:
                        attachment_content.append(None)
                    attachment_names.append(attachment_file_name)
                    attachment_content_ids.append(attachment_content_id)
                    attachment_content_dispositions.append(attachment_content_disposition)
                else:
                    # .msg and other files (png, jpeg)
                    if part.is_multipart() and max_depth - 1 > 0:
                        # email is DSN
                        msgs = part.get_payload()  # human-readable section
                        for i, individual_message in enumerate(msgs):
                            msg_info = decode_attachment_payload(individual_message)
                            attached_emails.append(msg_info)

                            attachment_file_name = individual_message.get_filename()
                            attachment_content_id = individual_message.get('Content-ID')
                            attachment_content_disposition = individual_message.get('Content-Disposition')
                            if attachment_file_name is None:
                                attachment_file_name = "unknown_file_name{}".format(i)

                            attachment_content.append(msg_info)
                            attachment_names.append(attachment_file_name)
                            attachment_content_ids.append(attachment_content_id)
                            attachment_content_dispositions.append(attachment_content_disposition)
                    else:
                        file_content = part.get_payload(decode=True)
                        if attachment_file_name.endswith('.p7s') or not file_content:
                            attachment_content.append(None)
                        # fileResult will return an error if file_content is None.
                        if file_content and not attachment_file_name.endswith('.p7s'):
                            attachment_content.append(file_content)
                            if attachment_file_name.endswith(('.png', '.jpg', '.jpeg', '.gif')):
                                attachments_images.append((attachment_content_id, part.get_payload().strip()))

                        if attachment_file_name.endswith(".msg") and max_depth - 1 > 0:
                            if file_content:
                                attachment_content.append(file_content)
                            f = tempfile.NamedTemporaryFile(delete=False)
                            try:
                                f.write(file_content)
                                f.close()
                                inner_msg, inner_attached_emails = handle_msg(f.name, attachment_file_name, False,
                                                                              max_depth - 1, original_depth)
                                if inner_msg:
                                    inner_msg['ParentFileName'] = file_name
                                attached_emails.append(inner_msg)
                                attached_emails.extend(inner_attached_emails)

                            finally:
                                os.remove(f.name)

                        attachment_names.append(attachment_file_name)
                        attachment_content_ids.append(attachment_content_id)
                        attachment_content_dispositions.append(attachment_content_disposition)

            elif part.get_content_type() == 'text/html':
                # This line replaces a new line that starts with `..` to a newline that starts with `.`
                # This is because SMTP duplicate dots for lines that start with `.` and get_payload() doesn't format
                # this correctly
                part.set_payload(part.get_payload().replace('=\r\n..', '=\r\n.'))
                part.set_payload(part.get_payload().replace('=\n..', '=\n.'))

                html = decode_content(part)

            elif part.get_content_type() == 'text/plain':
                text = decode_content(part)

        if attachments_images and html:  # embed images into the HTML body.
            html = embed_images_to_html_body(html=html, attachments_images=attachments_images)

        email_data = None
        # if we are parsing a signed attachment there can be one of two options:
        # 1. it is 'multipart/signed' so it is probably a wrapper and we can ignore the outer "email"
        # 2. if it is 'multipart/signed' but has 'to' address so it is actually a real mail.
        if 'multipart/signed' not in eml.get_content_type() \
                or ('multipart/signed' in eml.get_content_type() and
                    (extract_address_eml(eml, 'to') or extract_address_eml(eml, 'from') or eml.get('subject'))):
            email_data = {
                'To': extract_address_eml(eml, 'to'),
                'CC': extract_address_eml(eml, 'cc'),
                'From': extract_address_eml(eml, 'from'),
                'Subject': convert_to_unicode(unfold(eml['Subject'])),
                'HTML': convert_to_unicode(html, is_msg_header=False),
                'Text': convert_to_unicode(text, is_msg_header=False),
                'HeadersMap': headers_map,
                'Attachments': ','.join(attachment_names) if attachment_names else '',
                'AttachmentNames': attachment_names if attachment_names else [],
                'AttachmentsData': [
                    {
                        "Name": attachment_names[i],
                        "Content-ID": attachment_content_ids[i],
                        "Content-Disposition": attachment_content_dispositions[i],
                        "FileData": attachment_content[i]
                    } for i in range(len(attachment_names))
                ],
                'Format': eml.get_content_type(),
                'Depth': original_depth - max_depth,
                'FileName': file_name
            }
        return email_data, attached_emails


def embed_images_to_html_body(html, attachments_images):
    """
    Embed images into the HTML body by changing the src of the image to the image content in base64

    Args:
        html (str): the HTML of the email.
        attachments_images (List(tuple[str, str])): a list of tuples containing attachment IDs and the image content in
            base64.

    https://sendgrid.com/blog/embedding-images-emails-facts/

    Returns:
        str: the HTML embedded with images.
    """
    if '<img' not in html:
        return html

    for attachment_id, image_base64 in attachments_images:
        if attachment_id:  # in p7m files types we can have png files without an attachment ID.
            attachment_id = re.sub('<|>', '', attachment_id)  # remove < and > from the attachment-ID.
            # '<image001.jpg@01D8B147.CFCD4400>' --> image001.jpg@01D8B147.CFCD4400
            image_base64 = re.sub('\n|\r', '', image_base64)  # remove escaping chars
            attachment_cid_pattern = f'src="cid:{attachment_id}"'
            if attachment_cid_pattern in html:
                html = html.replace(
                    attachment_cid_pattern, f'src="data:image/jpeg;base64,{image_base64}"'
                )
    return html


def unfold(s):
    r"""
    Remove folding whitespace from a string by converting line breaks (and any
    whitespace adjacent to line breaks) to a single space and removing leading
    & trailing whitespace.
    From: https://github.com/jwodder/headerparser/blob/master/headerparser/types.py#L39
    unfold('This is a \n folded string.\n')
    'This is a folded string.'
    :param string s: a string to unfold
    :rtype: string
    """
    return re.sub(r'[ \t]*[\r\n][ \t\r\n]*', ' ', s).strip(' ') if s else s


def decode_content(mime):
    """
      Decode content
    """
    charset = mime.get_content_charset()
    payload = mime.get_payload(decode=True)
    try:
        if payload:
            if charset == 'ascii':
                return payload.decode("ascii")
            elif charset == 'iso-8859-2':
                return payload.decode('iso-8859-2')
            elif charset == 'utf-8':
                return payload.decode('utf-8')
            else:
                return payload.decode("raw-unicode-escape")
        else:
            return ''

    except UnicodeDecodeError:
        payload = mime.get_payload()
        if isinstance(payload, str):
            return payload


def handle_SMTP_headers(emlFile):
    """
    Remove the transfer headers attached to the eml file by the SMTP protocol. The function reads the lines of the input
    eml file until a line which isn't an SMTP header is reached.
    """
    SMTP_HEADERS = ['MAIL FROM', 'RCPT TO', 'DATA']
    remove_smtp_header = True
    while remove_smtp_header:
        pos = emlFile.tell()
        line = emlFile.readline()
        if not any(smtp_header in str(line) for smtp_header in SMTP_HEADERS):
            remove_smtp_header = False
            emlFile.seek(pos)


def mime_decode(word_mime_encoded):
    prefix, charset, encoding, encoded_text, suffix = word_mime_encoded.groups()
    if encoding.lower() == 'b':
        byte_string = base64.b64decode(encoded_text)
    elif encoding.lower() == 'q':
        byte_string = quopri.decodestring(encoded_text, header=True)
    return prefix + byte_string.decode(charset) + suffix


def get_email_address(eml, entry):
    """
    This function gets email addresses from an eml object, i.e eml[entry].
    Args:
        eml : Email object.
        entry (str) : entry to look for in the email. i.e ('To', 'CC', 'From')
    Returns:
        res (str) : string of all required email addresses.
    """
    if entry == 'from':
        gel_all_values_from_email_by_entry = [str(current_eml_no_newline).replace('\r\n', '').replace('\n', '')
                                              for current_eml_no_newline in eml.get_all(entry, [])]
    else:
        gel_all_values_from_email_by_entry = eml.get_all(entry, [])
    addresses = getaddresses(gel_all_values_from_email_by_entry)
    if addresses:
        res = [item[1] for item in addresses]
        res = ', '.join(res)
        return res
    return ''


def extract_address_eml(eml, entry):
    """
    This function calls get_email_address in order to get required email addresses from email object.
    In addition, this function handles an edge case of '\r\n' in eml['from'] (as explained below).
    Args:
        eml : Email object.
        entry (str) : entry to look for in the email. i.e ('To', 'CC', 'From')
    Returns:
        res (str) : string of all required email addresses.
    """
    email_address = get_email_address(eml, entry)
    if email_address:
        return email_address
    else:
        return ''


def get_attachment_filename(part):
    attachment_file_name = None
    if part.get_filename():
        attachment_file_name = part.get_filename()

    elif attachment_file_name is None and part.get('filename'):
        attachment_file_name = os.path.normpath(part.get('filename'))
        if os.path.isabs(attachment_file_name):
            attachment_file_name = os.path.basename(attachment_file_name)
    else:
        for payload in part.get_payload():
            if payload.get_filename():
                attachment_file_name = payload.get_filename()
                break

    return attachment_file_name


def decode_attachment_payload(message):
    """Decodes a message from Base64, if fails will outputs its str(message)
    """
    msg = message.get_payload()
    try:
        # In some cases the body content is empty and cannot be decoded.
        msg_info = base64.b64decode(msg)
    except TypeError:
        msg_info = str(msg)
    return msg_info
