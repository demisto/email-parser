import base64
import email
import email.utils
import logging
import os
import quopri
import re
import tempfile
from base64 import b64decode
from email import errors, message_from_string
from email.header import decode_header, make_header
from email.message import Message
from email.parser import HeaderParser
from email.utils import getaddresses

from parse_emails.common import convert_to_unicode
from parse_emails.handle_msg import handle_msg

logger = logging.getLogger('parse_emails')

MIME_ENCODED_WORD = re.compile(r'(.*)=\?(.+)\?([B|Q])\?(.+)\?=(.*)')  # guardrails-disable-line
ENCODINGS_TYPES = {'utf-8', 'iso8859-1'}
headerRE = re.compile(r'^(From |[\041-\071\073-\176]*:|[\t ])')


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

        logger.debug(f'Before check_if_file_starts_with_header, {file_data=}')
        file_data = check_if_file_starts_with_header(file_data)
        logger.debug(f'After check_if_file_starts_with_header, {file_data=}')

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

        eml = None
        try:
            eml = create_message_from_string(file_data)
        except Exception as e:
            logger.info(f'Exception calling create_message_from_string, {e}, from {file_data=}')

        if not eml:
            logger.info('Empty eml after create_message_from_string')
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
            print("handle_eml, while parts")
            part = parts.pop()
            logger.debug(f'Iterating over parts. Current part: {part.get_content_type()=}')
            if (part.is_multipart() or part.get_content_type().startswith('multipart')) \
                    and "attachment" not in part.get("Content-Disposition", ""):
                parts += [part_ for part_ in part.get_payload() if isinstance(part_, email.message.Message)]

            elif part.get_filename()\
                    or "attachment" in part.get("Content-Disposition", "")\
                    or part.get("X-Attachment-Id")\
                    or ("image" in part.get("Content-Type", '') and part.get("Content-Transfer-Encoding") == "base64"):

                attachment_content_id = part.get('Content-ID')
                attachment_content_disposition = part.get('Content-Disposition')
                attachment_file_name = get_attachment_filename(part)
                # if attachment_file_name:
                #     attachment_names.append(attachment_file_name)
                print(f"handle_eml, attachment_file_name = get_attachment_filename(part), {attachment_file_name=}")

                if attachment_file_name is None and part.get('filename'):
                    attachment_file_name = os.path.normpath(part.get('filename'))
                    # if attachment_file_name:
                    #     attachment_names.append(attachment_file_name)
                    print(f"handle_eml, if attachment_file_name is None and part.get('filename'), {attachment_file_name=}")
                    if os.path.isabs(attachment_file_name):
                        attachment_file_name = os.path.basename(attachment_file_name)
                        # if attachment_file_name:
                        #     attachment_names.append(attachment_file_name)
                        print(f"handle_eml, if os.path.isabs(attachment_file_name), {attachment_file_name=}")

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
                            # if attachment_file_name:
                            #     attachment_names.append(attachment_file_name)
                            print(f"handle_eml, attachment_file_name = f'__attachment_name__.eml', {attachment_file_name=}")

                        file_content = part.get_payload()[0].as_string().strip()
                        if base64_encoded:
                            try:
                                file_content = b64decode(file_content)

                            except TypeError:
                                pass  # In case the file is a string, decode=True for get_payload is not working

                    elif isinstance(part.get_payload(), str):
                        file_content = part.get_payload(decode=True)
                    else:
                        logger.debug("found eml attachment with Content-Type=message/rfc822 but has no payload")

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
                        # email is DSN/Multipart
                        msgs = part.get_payload()  # human-readable section
                        for i, individual_message in enumerate(msgs):
                            print('for i, individual_message in enumerate(msgs)')
                            msg_info = decode_attachment_payload(individual_message)

                            attachment_file_name = individual_message.get_filename()
                            # if attachment_file_name:
                            #     attachment_names.append(attachment_file_name)
                            print(f"handle_eml, attachment_file_name = individual_message.get_filename(), {attachment_file_name=}")
                            attachment_content_id = individual_message.get('Content-ID')
                            attachment_content_disposition = individual_message.get('Content-Disposition')
                            if attachment_file_name is None:
                                attachment_file_name = f"unknown_file_name{i}"
                                # if attachment_file_name:
                                #     attachment_names.append(attachment_file_name)
                                print(f"handle_eml, attachment_file_name = funknown_file_name__i__, {attachment_file_name=}")

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
                                inner_msg, inner_attached_emails, attached_eml = handle_msg(f.name, attachment_file_name, False,
                                                                                            max_depth - 1, original_depth)
                                if attached_eml:
                                    attached_eml = parse_inner_eml(attachments=attached_eml,
                                                                   original_depth=original_depth)
                                    attached_emails += attached_eml
                                if inner_msg:
                                    inner_msg['ParentFileName'] = file_name
                                attached_emails.append(inner_msg)
                                attached_emails.extend(inner_attached_emails)

                            finally:
                                os.remove(f.name)

                        attachment_names.append(attachment_file_name)
                        attachment_content_ids.append(attachment_content_id)
                        attachment_content_dispositions.append(attachment_content_disposition)
                # if attachment_file_name:
                #     attachment_names.append(attachment_file_name)
                print(f'attachment_names before removing duplicates: {attachment_names}')
                attachment_names = list(dict.fromkeys(attachment_names))
                print(f'attachment_names after removing duplicates: {attachment_names}')

            elif part.get_content_type() == 'text/html':
                # This line replaces a new line that starts with `..` to a newline that starts with `.`
                # This is because SMTP duplicate dots for lines that start with `.` and get_payload() doesn't format
                # this correctly
                part.set_payload(part.get_payload().replace('=\r\n..', '=\r\n.'))
                part.set_payload(part.get_payload().replace('=\n..', '=\n.'))

                html = decode_content(part)

            elif part.get_content_type() == 'text/plain':
                text = decode_content(part)
            else:
                logger.info(f'Not handling part of type {part.get_content_type()=}')

        email_data = None
        # if we are parsing a signed attachment there can be one of two options:
        # 1. it is 'multipart/signed' so it is probably a wrapper, and we can ignore the outer "email"
        #    However, we should save its AttachmentsData.
        # 2. if it is 'multipart/signed' but has 'to'/'from'/'subject' fields, so it is actually a real mail.
        if 'multipart/signed' not in eml.get_content_type() \
            or ('multipart/signed' in eml.get_content_type() and
                ((extract_address_eml(eml, 'to') or extract_address_eml(eml, 'from') or eml.get('subject')) or
                 attachment_names)):
            email_data = {
                'To': extract_address_eml(eml, 'to'),
                'CC': extract_address_eml(eml, 'cc'),
                'BCC': extract_address_eml(eml, 'bcc'),
                'From': extract_address_eml(eml, 'from'),
                'Subject': convert_to_unicode(unfold(eml['Subject'])),
                'HTML': convert_to_unicode(html, is_msg_header=False),
                'Text': convert_to_unicode(text, is_msg_header=False),
                'HeadersMap': headers_map,
                'Headers': header_list,
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


def create_message_from_string(file_data: str) -> Message:
    """
    Parse a string into a Message object model.
    and checks if there is a multipart error we try to fix it
    Args:
        file_data (str) : the email data as string.
    Returns:
        the eml parse obj (Message)
    """
    eml = message_from_string(file_data)
    if eml.defects:
        eml = handle_multi_part_error(eml)
    return eml


def handle_multi_part_error(eml: Message):
    """
    This function handles a multipart which is missing boundary,
    and checks if the boundary exists only once in the file, if so it removes it.
    Args:
        the eml parse obj (Message) : the email data.
    Returns:
        the eml parse obj (Message)
    """
    logger.debug('handle_multi_part_error')
    for defect in eml.defects:
        if isinstance(defect, errors.MultipartInvariantViolationDefect):
            boundary = eml.get_boundary()
            file_data = eml.as_string()
            if file_data.count(boundary) == 1:
                param = eml.get("Content-Type").replace('\r\n', '\n')
                file_data = file_data.replace(f'Content-Type: {param}\n', '')
                eml = message_from_string(file_data)
            break
    return eml


def check_if_file_starts_with_header(file_data: str):
    """
    This function checks if the file data starts with headers, and if not it deletes the lines before the headers.
    Args:
        file_data (str) : the email data.
    Returns:
        file_data (str) : the email data without text before the headers.
    """
    for idx, line in enumerate(file_data.splitlines()):
        if headerRE.match(line):
            file_data = file_data.split("\n", idx)[idx]
            break
    return file_data


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
    content_transfer_encoding = mime.get('content-transfer-encoding', '').rstrip(' ')
    if (charset == 'utf-8' and content_transfer_encoding == '8bit'):
        return mime.get_payload()
    payload = mime.get_payload(decode=True)
    try:
        if payload:
            if charset == 'ascii':
                return payload.decode("ascii")
            elif charset == 'iso-8859-2':
                return payload.decode('iso-8859-2')
            elif charset == 'utf-8':
                return payload.decode('utf-8', errors='ignore')
            elif charset in ('gb2312', 'gb18030'):  # chinese encodings
                return payload.decode('gb18030')
            elif charset == 'iso-2022-jp':
                return payload.decode('iso-2022-jp')
            elif charset == 'big5':
                return payload.decode('big5')
            elif charset == 'gbk':
                return payload.decode('gbk')
            else:
                return payload.decode("raw-unicode-escape")
        else:
            logger.debug('decode_content, empty payload, returning an empty string.')
            return ''

    except UnicodeDecodeError as ude:
        logger.info(f'Exception trying to decode content: {ude}')
        payload = mime.get_payload()
        if isinstance(payload, str):
            logger.info(f'Exception trying to decode content. payload is str, returning it. {ude}')
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
        res = [email_address for real_name, email_address in addresses if "@" in email_address]
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
        attachment_file_name = str(make_header(decode_header(part.get_filename())))
        print(f'get_attachment_filename, if part.get_filename(), {attachment_file_name=}')

    elif attachment_file_name is None and part.get('filename'):
        attachment_file_name = os.path.normpath(part.get('filename'))
        print(f"get_attachment_filename, elif attachment_file_name is None and part.get('filename'), {attachment_file_name=}")
        if os.path.isabs(attachment_file_name):
            attachment_file_name = os.path.basename(attachment_file_name)
            print(f"get_attachment_filename, if os.path.isabs(attachment_file_name), {attachment_file_name=}")
    else:
        if attach_id := part.get("X-Attachment-Id"):
            attachment_file_name = attach_id
            print(f"get_attachment_filename, if attach_id := part.get('X-Attachment-Id'), {attachment_file_name=}")
        elif not isinstance(part.get_payload(), list):
            attachment_file_name = 'unknown_file_name'
            print(f"get_attachment_filename, elif not isinstance(part.get_payload(), list), {attachment_file_name=}")
        else:
            for payload in part.get_payload():
                if payload.get_filename():
                    attachment_file_name = payload.get_filename()
                    print(f"get_attachment_filename, if payload.get_filename(), {attachment_file_name=}")
                    break

    print(f"get_attachment_filename, returning {attachment_file_name=}")
    return attachment_file_name


def decode_attachment_payload(message):
    """Decodes a message from Base64, if fails will outputs its str(message)
    """
    msg = message.get_payload()
    try:
        # In some cases the body content is empty and cannot be decoded.
        msg_info = base64.b64decode(msg)
    except Exception as e:
        logger.debug(f'exception while trying to decode_attachment_payload - {str(e)}')
        msg_info = str(msg)
    return msg_info


def parse_inner_eml(attachments, original_depth):
    attached_emls = []
    for attachment in attachments:
        tf = tempfile.NamedTemporaryFile(delete=False)

        try:
            tf.write(attachment.get('data'))
            tf.close()

            inner_eml, attached_inner_emails = handle_eml(tf.name, file_name=attachment.get('name'),
                                                          max_depth=attachment.get('max_depth'),
                                                          original_depth=original_depth)
            if inner_eml:
                attached_emls.append(inner_eml)
            if attached_inner_emails:
                attached_emls.extend(attached_inner_emails)
        finally:
            os.remove(tf.name)

    return attached_emls
