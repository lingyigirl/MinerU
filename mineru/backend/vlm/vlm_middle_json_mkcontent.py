import os
import uuid

from bs4 import BeautifulSoup
from loguru import logger

from mineru.utils.char_utils import full_to_half_exclude_marks, is_hyphen_at_line_end
from mineru.utils.config_reader import get_latex_delimiter_config, get_formula_enable, get_table_enable
from mineru.utils.enum_class import MakeMode, BlockType, ContentType, ContentTypeV2
from mineru.utils.language import detect_lang

latex_delimiters_config = get_latex_delimiter_config()

default_delimiters = {
    'display': {'left': '$$', 'right': '$$'},
    'inline': {'left': '$', 'right': '$'}
}

delimiters = latex_delimiters_config if latex_delimiters_config else default_delimiters

display_left_delimiter = delimiters['display']['left']
display_right_delimiter = delimiters['display']['right']
inline_left_delimiter = delimiters['inline']['left']
inline_right_delimiter = delimiters['inline']['right']


def merge_para_with_text(para_block, formula_enable=True, img_buket_path=''):
    block_text = ''
    for line in para_block['lines']:
        for span in line['spans']:
            if span['type'] in [ContentType.TEXT]:
                span['content'] = full_to_half_exclude_marks(span['content'])
                block_text += span['content']
    block_lang = detect_lang(block_text)

    para_text = ''
    for i, line in enumerate(para_block['lines']):
        for j, span in enumerate(line['spans']):
            span_type = span['type']
            content = ''
            if span_type == ContentType.TEXT:
                content = span['content']
            elif span_type == ContentType.INLINE_EQUATION:
                content = f"{inline_left_delimiter}{span['content']}{inline_right_delimiter}"
            elif span_type == ContentType.INTERLINE_EQUATION:
                if formula_enable:
                    content = f"\n{display_left_delimiter}\n{span['content']}\n{display_right_delimiter}\n"
                else:
                    if span.get('image_path', ''):
                        content = f"![]({img_buket_path}/{span['image_path']})"

            content = content.strip()
            if content:

                if span_type == ContentType.INTERLINE_EQUATION:
                    para_text += content
                    continue

                # 定义CJK语言集合(中日韩)
                cjk_langs = {'zh', 'ja', 'ko'}
                # logger.info(f'block_lang: {block_lang}, content: {content}')

                # 判断是否为行末span
                is_last_span = j == len(line['spans']) - 1

                if block_lang in cjk_langs:  # 中文/日语/韩文语境下，换行不需要空格分隔,但是如果是行内公式结尾，还是要加空格
                    if is_last_span and span_type != ContentType.INLINE_EQUATION:
                        para_text += content
                    else:
                        para_text += f'{content} '
                else:
                    # 西方文本语境下 每行的最后一个span判断是否要去除连字符
                    if span_type in [ContentType.TEXT, ContentType.INLINE_EQUATION]:
                        # 如果span是line的最后一个且末尾带有-连字符，那么末尾不应该加空格,同时应该把-删除
                        if (
                                is_last_span
                                and span_type == ContentType.TEXT
                                and is_hyphen_at_line_end(content)
                        ):
                            # 如果下一行的第一个span是小写字母开头，删除连字符
                            if (
                                    i+1 < len(para_block['lines'])
                                    and para_block['lines'][i + 1].get('spans')
                                    and para_block['lines'][i + 1]['spans'][0].get('type') == ContentType.TEXT
                                    and para_block['lines'][i + 1]['spans'][0].get('content', '')
                                    and para_block['lines'][i + 1]['spans'][0]['content'][0].islower()
                            ):
                                para_text += content[:-1]
                            else:  # 如果没有下一行，或者下一行的第一个span不是小写字母开头，则保留连字符但不加空格
                                para_text += content
                        else:  # 西方文本语境下 content间需要空格分隔
                            para_text += f'{content} '
    return para_text


def mk_blocks_to_markdown(para_blocks, make_mode, formula_enable, table_enable, img_buket_path=''):
    page_markdown = []
    for para_block in para_blocks:
        para_text = ''
        para_type = para_block['type']
        if para_type in [BlockType.TEXT, BlockType.INTERLINE_EQUATION, BlockType.PHONETIC, BlockType.REF_TEXT]:
            para_text = merge_para_with_text(para_block, formula_enable=formula_enable, img_buket_path=img_buket_path)
        elif para_type == BlockType.LIST:
            for block in para_block['blocks']:
                item_text = merge_para_with_text(block, formula_enable=formula_enable, img_buket_path=img_buket_path)
                para_text += f"{item_text}  \n"
        elif para_type == BlockType.TITLE:
            title_level = get_title_level(para_block)
            para_text = f'{"#" * title_level} {merge_para_with_text(para_block)}'
        elif para_type == BlockType.IMAGE:
            if make_mode == MakeMode.NLP_MD:
                continue
            elif make_mode == MakeMode.MM_MD:
                # 检测是否存在图片脚注
                has_image_footnote = any(block['type'] == BlockType.IMAGE_FOOTNOTE for block in para_block['blocks'])
                # 如果存在图片脚注，则将图片脚注拼接到图片正文后面
                if has_image_footnote:
                    for block in para_block['blocks']:  # 1st.拼image_caption
                        if block['type'] == BlockType.IMAGE_CAPTION:
                            para_text += merge_para_with_text(block) + '  \n'
                    for block in para_block['blocks']:  # 2nd.拼image_body
                        if block['type'] == BlockType.IMAGE_BODY:
                            for line in block['lines']:
                                for span in line['spans']:
                                    if span['type'] == ContentType.IMAGE:
                                        if span.get('image_path', ''):
                                            para_text += f"![]({img_buket_path}/{span['image_path']})"
                    for block in para_block['blocks']:  # 3rd.拼image_footnote
                        if block['type'] == BlockType.IMAGE_FOOTNOTE:
                            para_text += '  \n' + merge_para_with_text(block)
                else:
                    for block in para_block['blocks']:  # 1st.拼image_body
                        if block['type'] == BlockType.IMAGE_BODY:
                            for line in block['lines']:
                                for span in line['spans']:
                                    if span['type'] == ContentType.IMAGE:
                                        if span.get('image_path', ''):
                                            para_text += f"![]({img_buket_path}/{span['image_path']})"
                    for block in para_block['blocks']:  # 2nd.拼image_caption
                        if block['type'] == BlockType.IMAGE_CAPTION:
                            para_text += '  \n' + merge_para_with_text(block)

        elif para_type == BlockType.TABLE:
            if make_mode == MakeMode.NLP_MD:
                continue
            elif make_mode == MakeMode.MM_MD:
                for block in para_block['blocks']:  # 1st.拼table_caption
                    if block['type'] == BlockType.TABLE_CAPTION:
                        para_text += merge_para_with_text(block) + '  \n'
                for block in para_block['blocks']:  # 2nd.拼table_body
                    if block['type'] == BlockType.TABLE_BODY:
                        for line in block['lines']:
                            for span in line['spans']:
                                if span['type'] == ContentType.TABLE:
                                    # if processed by table model
                                    if table_enable:
                                        if span.get('html', ''):
                                            para_text += f"\n{span['html']}\n"
                                        elif span.get('image_path', ''):
                                            para_text += f"![]({img_buket_path}/{span['image_path']})"
                                    else:
                                        if span.get('image_path', ''):
                                            para_text += f"![]({img_buket_path}/{span['image_path']})"
                for block in para_block['blocks']:  # 3rd.拼table_footnote
                    if block['type'] == BlockType.TABLE_FOOTNOTE:
                        para_text += '\n' + merge_para_with_text(block) + '  '
        elif para_type == BlockType.CODE:
            sub_type = para_block["sub_type"]
            for block in para_block['blocks']:  # 1st.拼code_caption
                if block['type'] == BlockType.CODE_CAPTION:
                    para_text += merge_para_with_text(block) + '  \n'
            for block in para_block['blocks']:  # 2nd.拼code_body
                if block['type'] == BlockType.CODE_BODY:
                    if sub_type == BlockType.CODE:
                        guess_lang = para_block["guess_lang"]
                        para_text += f"```{guess_lang}\n{merge_para_with_text(block)}\n```"
                    elif sub_type == BlockType.ALGORITHM:
                        para_text += merge_para_with_text(block)

        if para_text.strip() == '':
            continue
        else:
            # page_markdown.append(para_text.strip() + '  ')
            page_markdown.append(para_text.strip())

    return page_markdown


def make_blocks_to_content_list(para_block, img_buket_path, page_idx, page_size):
    para_type = para_block['type']
    para_content = {}
    if para_type in [
        BlockType.TEXT,
        BlockType.REF_TEXT,
        BlockType.PHONETIC,
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.ASIDE_TEXT,
        BlockType.PAGE_FOOTNOTE,
    ]:
        para_content = {
            'type': para_type,
            'text': merge_para_with_text(para_block),
        }
    elif para_type == BlockType.LIST:
        para_content = {
            'type': para_type,
            'sub_type': para_block.get('sub_type', ''),
            'list_items':[],
        }
        for block in para_block['blocks']:
            item_text = merge_para_with_text(block)
            if item_text.strip():
                para_content['list_items'].append(item_text)
    elif para_type == BlockType.TITLE:
        title_level = get_title_level(para_block)
        para_content = {
            'type': ContentType.TEXT,
            'text': merge_para_with_text(para_block),
        }
        if title_level != 0:
            para_content['text_level'] = title_level
    elif para_type == BlockType.INTERLINE_EQUATION:
        para_content = {
            'type': ContentType.EQUATION,
            'text': merge_para_with_text(para_block),
            'text_format': 'latex',
        }
    elif para_type == BlockType.IMAGE:
        para_content = {'type': ContentType.IMAGE, 'img_path': '', BlockType.IMAGE_CAPTION: [], BlockType.IMAGE_FOOTNOTE: []}
        for block in para_block['blocks']:
            if block['type'] == BlockType.IMAGE_BODY:
                for line in block['lines']:
                    for span in line['spans']:
                        if span['type'] == ContentType.IMAGE:
                            if span.get('image_path', ''):
                                para_content['img_path'] = f"{img_buket_path}/{span['image_path']}"
            if block['type'] == BlockType.IMAGE_CAPTION:
                para_content[BlockType.IMAGE_CAPTION].append(merge_para_with_text(block))
            if block['type'] == BlockType.IMAGE_FOOTNOTE:
                para_content[BlockType.IMAGE_FOOTNOTE].append(merge_para_with_text(block))
    elif para_type == BlockType.TABLE:
        para_content = {'type': ContentType.TABLE, 'img_path': '', BlockType.TABLE_CAPTION: [], BlockType.TABLE_FOOTNOTE: []}
        for block in para_block['blocks']:
            if block['type'] == BlockType.TABLE_BODY:
                for line in block['lines']:
                    for span in line['spans']:
                        if span['type'] == ContentType.TABLE:

                            if span.get('html', ''):
                                para_content[BlockType.TABLE_BODY] = f"{span['html']}"

                            if span.get('image_path', ''):
                                para_content['img_path'] = f"{img_buket_path}/{span['image_path']}"

            if block['type'] == BlockType.TABLE_CAPTION:
                para_content[BlockType.TABLE_CAPTION].append(merge_para_with_text(block))
            if block['type'] == BlockType.TABLE_FOOTNOTE:
                para_content[BlockType.TABLE_FOOTNOTE].append(merge_para_with_text(block))
    elif para_type == BlockType.CODE:
        para_content = {'type': BlockType.CODE, 'sub_type': para_block["sub_type"], BlockType.CODE_CAPTION: []}
        for block in para_block['blocks']:
            if block['type'] == BlockType.CODE_BODY:
                code_text = merge_para_with_text(block)
                if para_block['sub_type'] == BlockType.CODE:
                    guess_lang = para_block.get("guess_lang", "txt")
                    code_text = f"```{guess_lang}\n{code_text}\n```"
                para_content[BlockType.CODE_BODY] = code_text
            if block['type'] == BlockType.CODE_CAPTION:
                para_content[BlockType.CODE_CAPTION].append(merge_para_with_text(block))

    page_width, page_height = page_size
    para_bbox = para_block.get('bbox')
    if para_bbox:
        x0, y0, x1, y1 = para_bbox
        para_content['bbox'] = [
            int(x0 * 1000 / page_width),
            int(y0 * 1000 / page_height),
            int(x1 * 1000 / page_width),
            int(y1 * 1000 / page_height),
        ]

    para_content['page_idx'] = page_idx

    return para_content


def make_blocks_to_content_list2(para_block, img_buket_path, page_idx, page_size,output_content):
    para_type = para_block['type']
    para_content = {}
    if para_type in [
        BlockType.TEXT,
        BlockType.REF_TEXT,
        BlockType.PHONETIC,
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.ASIDE_TEXT,
        BlockType.PAGE_FOOTNOTE,
    ]:
        para_content = {
            'type': para_type,
            'text': merge_para_with_text(para_block),
        }
    elif para_type == BlockType.TITLE:
        title_level = get_title_level(para_block)
        para_content = {
            'type': ContentType.TEXT,
            'text': merge_para_with_text(para_block),
        }
        if title_level != 0:
            para_content['text_level'] = title_level
    elif para_type == BlockType.INTERLINE_EQUATION:
        para_content = {
            'type': ContentType.EQUATION,
            'text': merge_para_with_text(para_block),
            'text_format': 'latex',
        }
    elif para_type == BlockType.IMAGE:
        para_content = {'type': ContentType.IMAGE, 'img_path': '', BlockType.IMAGE_CAPTION: [], BlockType.IMAGE_FOOTNOTE: []}
        for block in para_block['blocks']:
            if block['type'] == BlockType.IMAGE_BODY:
                for line in block['lines']:
                    for span in line['spans']:
                        if span['type'] == ContentType.IMAGE:
                            if span.get('image_path', ''):
                                para_content['img_path'] = f"{img_buket_path}/{span['image_path']}"
            if block['type'] == BlockType.IMAGE_CAPTION:
                para_content[BlockType.IMAGE_CAPTION].append(merge_para_with_text(block))
            if block['type'] == BlockType.IMAGE_FOOTNOTE:
                para_content[BlockType.IMAGE_FOOTNOTE].append(merge_para_with_text(block))
    elif para_type == BlockType.CODE:
        para_content = {'type': BlockType.CODE, 'sub_type': para_block["sub_type"], BlockType.CODE_CAPTION: []}
        for block in para_block['blocks']:
            if block['type'] == BlockType.CODE_BODY:
                code_text = merge_para_with_text(block)
                if para_block['sub_type'] == BlockType.CODE:
                    guess_lang = para_block.get("guess_lang", "txt")
                    code_text = f"```{guess_lang}\n{code_text}\n```"
                para_content[BlockType.CODE_BODY] = code_text
            if block['type'] == BlockType.CODE_CAPTION:
                para_content[BlockType.CODE_CAPTION].append(merge_para_with_text(block))
    elif para_type == BlockType.LIST:
        make_list_content(para_type, para_block, output_content, page_idx, page_size)
    elif para_type == BlockType.TABLE:
        for block in para_block['blocks']:
            # 需要拆成两个一个table_caption 和 table_body
            if block['type'] == BlockType.TABLE_BODY:
                make_table_body(block, para_block, img_buket_path, page_idx, page_size, output_content)

            if block['type'] == BlockType.TABLE_CAPTION:
                make_table_caption(block, para_block, img_buket_path, page_idx, page_size, output_content)

            if block['type'] == BlockType.TABLE_FOOTNOTE:
                make_table_footnote(block, para_block, img_buket_path, page_idx, page_size, output_content)

    if para_type != BlockType.LIST and para_type != BlockType.TABLE:
        para_content['chunk_id'] = str(uuid.uuid4())
        para_content = create_boox(para_content, para_block, page_idx, page_size)
        output_content.append(para_content)

    return para_content


def make_blocks_to_content_list_v2(para_block, img_buket_path, page_size):
    para_type = para_block['type']
    para_content = {}
    if para_type in [
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.ASIDE_TEXT,
        BlockType.PAGE_NUMBER,
        BlockType.PAGE_FOOTNOTE,
    ]:
        if para_type == BlockType.HEADER:
            content_type = ContentTypeV2.PAGE_HEADER
        elif para_type == BlockType.FOOTER:
            content_type = ContentTypeV2.PAGE_FOOTER
        elif para_type == BlockType.ASIDE_TEXT:
            content_type = ContentTypeV2.PAGE_ASIDE_TEXT
        elif para_type == BlockType.PAGE_NUMBER:
            content_type = ContentTypeV2.PAGE_NUMBER
        elif para_type == BlockType.PAGE_FOOTNOTE:
            content_type = ContentTypeV2.PAGE_FOOTNOTE
        else:
            raise ValueError(f"Unknown para_type: {para_type}")
        para_content = {
            'type': content_type,
            'content': {
                f"{content_type}_content": merge_para_with_text_v2(para_block),
            }
        }
    elif para_type == BlockType.TITLE:
        title_level = get_title_level(para_block)
        if title_level != 0:
            para_content = {
                'type': ContentTypeV2.TITLE,
                'content': {
                    "title_content": merge_para_with_text_v2(para_block),
                    "level": title_level
                }
            }
        else:
            para_content = {
                'type': ContentTypeV2.PARAGRAPH,
                'content': {
                    "paragraph_content": merge_para_with_text_v2(para_block),
                }
            }
    elif para_type in [
        BlockType.TEXT,
        BlockType.PHONETIC
    ]:
        para_content = {
            'type': ContentTypeV2.PARAGRAPH,
            'content': {
                'paragraph_content': merge_para_with_text_v2(para_block),
            }
        }
    elif para_type == BlockType.INTERLINE_EQUATION:
        image_path, math_content = get_body_data(para_block)
        para_content = {
            'type': ContentTypeV2.EQUATION_INTERLINE,
            'content': {
                'math_content': math_content,
                'math_type': 'latex',
                'image_source': {'path': f"{img_buket_path}/{image_path}"},
            }
        }
    elif para_type == BlockType.IMAGE:
        image_caption = []
        image_footnote = []
        image_path, _ = get_body_data(para_block)
        image_source = {
            'path': f"{img_buket_path}/{image_path}",
        }
        for block in para_block['blocks']:
            if block['type'] == BlockType.IMAGE_CAPTION:
                image_caption.extend(merge_para_with_text_v2(block))
            if block['type'] == BlockType.IMAGE_FOOTNOTE:
                image_footnote.extend(merge_para_with_text_v2(block))
        para_content = {
            'type': ContentTypeV2.IMAGE,
            'content': {
                'image_source': image_source,
                'image_caption': image_caption,
                'image_footnote': image_footnote,
            }
        }
    elif para_type == BlockType.TABLE:
        table_caption = []
        table_footnote = []
        image_path, html = get_body_data(para_block)
        image_source = {
            'path': f"{img_buket_path}/{image_path}",
        }
        if html.count("<table") > 1:
            table_nest_level = 2
        else:
            table_nest_level = 1
        if (
                "colspan" in html or
                "rowspan" in html or
                table_nest_level > 1
        ):
            table_type = ContentTypeV2.TABLE_COMPLEX
        else:
            table_type = ContentTypeV2.TABLE_SIMPLE

        for block in para_block['blocks']:
            if block['type'] == BlockType.TABLE_CAPTION:
                table_caption.extend(merge_para_with_text_v2(block))
            if block['type'] == BlockType.TABLE_FOOTNOTE:
                table_footnote.extend(merge_para_with_text_v2(block))
        para_content = {
            'type': ContentTypeV2.TABLE,
            'content': {
                'image_source': image_source,
                'table_caption': table_caption,
                'table_footnote': table_footnote,
                'html': html,
                'table_type': table_type,
                'table_nest_level': table_nest_level,
            }
        }
    elif para_type == BlockType.CODE:
        code_caption = []
        code_content = []
        for block in para_block['blocks']:
            if block['type'] == BlockType.CODE_CAPTION:
                code_caption.extend(merge_para_with_text_v2(block))
            if block['type'] == BlockType.CODE_BODY:
                code_content = merge_para_with_text_v2(block)
        sub_type = para_block["sub_type"]
        if sub_type == BlockType.CODE:
            para_content = {
                'type': ContentTypeV2.CODE,
                'content': {
                    'code_caption': code_caption,
                    'code_content': code_content,
                    'code_language': para_block.get('guess_lang', 'txt'),
                }
            }
        elif sub_type == BlockType.ALGORITHM:
            para_content = {
                'type': ContentTypeV2.ALGORITHM,
                'content': {
                    'algorithm_caption': code_caption,
                    'algorithm_content': code_content,
                }
            }
        else:
            raise ValueError(f"Unknown code sub_type: {sub_type}")
    elif para_type == BlockType.REF_TEXT:
        para_content = {
            'type': ContentTypeV2.LIST,
            'content': {
                'list_type': ContentTypeV2.LIST_REF,
                'list_items': [
                    {
                        'item_type': 'text',
                        'item_content': merge_para_with_text_v2(para_block),
                    }
                ],
            }
        }
    elif para_type == BlockType.LIST:
        if 'sub_type' in para_block:
            if para_block['sub_type'] == BlockType.REF_TEXT:
                list_type = ContentTypeV2.LIST_REF
            elif para_block['sub_type'] == BlockType.TEXT:
                list_type = ContentTypeV2.LIST_TEXT
            else:
                raise ValueError(f"Unknown list sub_type: {para_block['sub_type']}")
        else:
            list_type = ContentTypeV2.LIST_TEXT
        list_items = []
        for block in para_block['blocks']:
            item_content = merge_para_with_text_v2(block)
            if item_content:
                list_items.append({
                    'item_type': 'text',
                    'item_content': item_content,
                })
        para_content = {
            'type': ContentTypeV2.LIST,
            'content': {
                'list_type': list_type,
                'list_items': list_items,
            }
        }

    page_width, page_height = page_size
    para_bbox = para_block.get('bbox')
    if para_bbox:
        x0, y0, x1, y1 = para_bbox
        para_content['bbox'] = [
            int(x0 * 1000 / page_width),
            int(y0 * 1000 / page_height),
            int(x1 * 1000 / page_width),
            int(y1 * 1000 / page_height),
        ]

    return para_content





def get_body_data(para_block):
    """
    Extract image_path and html from para_block
    Returns:
        - For IMAGE/INTERLINE_EQUATION: (image_path, '')
        - For TABLE: (image_path, html)
        - Default: ('', '')
    """

    def get_data_from_spans(lines):
        for line in lines:
            for span in line.get('spans', []):
                span_type = span.get('type')
                if span_type == ContentType.TABLE:
                    return span.get('image_path', ''), span.get('html', '')
                elif span_type == ContentType.IMAGE:
                    return span.get('image_path', ''), ''
                elif span_type == ContentType.INTERLINE_EQUATION:
                    return span.get('image_path', ''), span.get('content', '')
                elif span_type == ContentType.TEXT:
                    return '', span.get('content', '')
        return '', ''

    # 处理嵌套的 blocks 结构
    if 'blocks' in para_block:
        for block in para_block['blocks']:
            block_type = block.get('type')
            if block_type in [BlockType.IMAGE_BODY, BlockType.TABLE_BODY, BlockType.CODE_BODY]:
                result = get_data_from_spans(block.get('lines', []))
                if result != ('', ''):
                    return result
        return '', ''

    # 处理直接包含 lines 的结构
    return get_data_from_spans(para_block.get('lines', []))


def merge_para_with_text_v2(para_block):
    block_text = ''
    for line in para_block['lines']:
        for span in line['spans']:
            if span['type'] in [ContentType.TEXT]:
                span['content'] = full_to_half_exclude_marks(span['content'])
                block_text += span['content']
    block_lang = detect_lang(block_text)

    para_content = []
    para_type = para_block['type']
    for i, line in enumerate(para_block['lines']):
        for j, span in enumerate(line['spans']):
            span_type = span['type']
            if span.get("content", '').strip():
                if span_type == ContentType.TEXT:
                    if para_type == BlockType.PHONETIC:
                        span_type = ContentTypeV2.SPAN_PHONETIC
                    else:
                        span_type = ContentTypeV2.SPAN_TEXT
                if span_type == ContentType.INLINE_EQUATION:
                    span_type = ContentTypeV2.SPAN_EQUATION_INLINE
                if span_type in [
                    ContentTypeV2.SPAN_TEXT,
                ]:
                    # 定义CJK语言集合(中日韩)
                    cjk_langs = {'zh', 'ja', 'ko'}
                    # logger.info(f'block_lang: {block_lang}, content: {content}')

                    # 判断是否为行末span
                    is_last_span = j == len(line['spans']) - 1

                    if block_lang in cjk_langs:  # 中文/日语/韩文语境下，换行不需要空格分隔,但是如果是行内公式结尾，还是要加空格
                        if is_last_span:
                            span_content = span['content']
                        else:
                            span_content = f"{span['content']} "
                    else:
                        # 如果span是line的最后一个且末尾带有-连字符，那么末尾不应该加空格,同时应该把-删除
                        if (
                                is_last_span
                                and is_hyphen_at_line_end(span['content'])
                        ):
                            # 如果下一行的第一个span是小写字母开头，删除连字符
                            if (
                                    i + 1 < len(para_block['lines'])
                                    and para_block['lines'][i + 1].get('spans')
                                    and para_block['lines'][i + 1]['spans'][0].get('type') == ContentType.TEXT
                                    and para_block['lines'][i + 1]['spans'][0].get('content', '')
                                    and para_block['lines'][i + 1]['spans'][0]['content'][0].islower()
                            ):
                                span_content = span['content'][:-1]
                            else:  # 如果没有下一行，或者下一行的第一个span不是小写字母开头，则保留连字符但不加空格
                                span_content = span['content']
                        else:
                            # 西方文本语境下content间需要空格分隔
                            span_content = f"{span['content']} "

                    if para_content and para_content[-1]['type'] == span_type:
                        # 合并相同类型的span
                        para_content[-1]['content'] += span_content
                    else:
                        span_content = {
                            'type': span_type,
                            'content': span_content,
                        }
                        para_content.append(span_content)

                elif span_type in [
                    ContentTypeV2.SPAN_PHONETIC,
                    ContentTypeV2.SPAN_EQUATION_INLINE,
                ]:
                    span_content = {
                        'type': span_type,
                        'content': span['content'],
                    }
                    para_content.append(span_content)
                else:
                    logger.warning(f"Unknown span type in merge_para_with_text_v2: {span_type}")
    return para_content


def union_make(pdf_info_dict: list,
               make_mode: str,
               img_buket_path: str = '',
               ):

    formula_enable = get_formula_enable(os.getenv('MINERU_VLM_FORMULA_ENABLE', 'True').lower() == 'true')
    table_enable = get_table_enable(os.getenv('MINERU_VLM_TABLE_ENABLE', 'True').lower() == 'true')

    output_content = []
    for page_info in pdf_info_dict:
        paras_of_layout = page_info.get('para_blocks')
        paras_of_discarded = page_info.get('discarded_blocks')
        page_idx = page_info.get('page_idx')
        page_size = page_info.get('page_size')
        if make_mode in [MakeMode.MM_MD, MakeMode.NLP_MD]:
            if not paras_of_layout:
                continue
            page_markdown = mk_blocks_to_markdown(paras_of_layout, make_mode, formula_enable, table_enable, img_buket_path)
            output_content.extend(page_markdown)
        elif make_mode == MakeMode.CONTENT_LIST:
            para_blocks = (paras_of_layout or []) + (paras_of_discarded or [])
            if not para_blocks:
                continue
            for para_block in para_blocks:
                # para_content = make_blocks_to_content_list(para_block, img_buket_path, page_idx, page_size)
                # output_content.append(para_content)
                para_block['chunk_id'] = str(uuid.uuid4())
                make_blocks_to_content_list2(para_block, img_buket_path, page_idx, page_size, output_content)

        elif make_mode == MakeMode.CONTENT_LIST_V2:
            # https://github.com/drunkpig/llm-webkit-mirror/blob/dev6/docs/specification/output_format/content_list_spec.md
            para_blocks = (paras_of_layout or []) + (paras_of_discarded or [])
            page_contents = []
            if para_blocks:
                for para_block in para_blocks:
                    para_content = make_blocks_to_content_list_v2(para_block, img_buket_path, page_size)
                    page_contents.append(para_content)
            output_content.append(page_contents)

    if make_mode in [MakeMode.MM_MD, MakeMode.NLP_MD]:
        return '\n\n'.join(output_content)
    elif make_mode in [MakeMode.CONTENT_LIST, MakeMode.CONTENT_LIST_V2]:
        return output_content
    return None


def get_title_level(block):
    title_level = block.get('level', 1)
    if title_level > 4:
        title_level = 4
    elif title_level < 1:
        title_level = 0
    return title_level




"""
    处理type:list的信息
"""
def make_list_content(para_type, para_block, output_content,page_idx, page_size):
    """
    para_list_content = {
        'type': para_type,
        'sub_type': para_block.get('sub_type', ''),
        'list_items': [],
    }
    """
    """
        para_content = {
            'type': para_type,
            'sub_type': para_block.get('sub_type', ''),
            'list_items': [],
        }
        for block in para_block['blocks']:
            item_text = merge_para_with_text(block)
            if item_text.strip():
                para_content['list_items'].append(item_text)
    """
    items = []
    for block in para_block['blocks']:
        item_text = merge_para_with_text(block)
        items.append(item_text)

    page_width, page_height = page_size
    para_bbox = para_block.get('bbox')
    if para_bbox:
        # x0, y0, x1, y1 = para_bbox
        items_bboxs = split_bbox(items, para_bbox)
        if items_bboxs:
            for item_bbox in items_bboxs:
                x0, y0, x1, y1 = item_bbox['bbox']
                item_bbox['type'] = BlockType.TEXT
                item_bbox['bbox'] = [
                    int(x0 * 1000 / page_width),
                    int(y0 * 1000 / page_height),
                    int(x1 * 1000 / page_width),
                    int(y1 * 1000 / page_height),
                ]
                item_bbox['page_idx'] = page_idx
                item_bbox['chunk_id'] = str(uuid.uuid4())
                output_content.append(item_bbox)


def split_bbox(items, bbox):
    # 提取整体bbox的坐标
    x0, y0, x1, y1 = bbox
    # 计算整体高度
    total_height = y1 - y0
    # 获取条目数量
    item_count = len(items)

    if item_count == 0:
        return []

    # 计算每个条目的高度
    item_height = total_height / item_count
    result = []

    # 为每个条目计算bbox
    for i, item in enumerate(items):
        item_y0 = y0 + i * item_height
        item_y1 = y0 + (i + 1) * item_height
        item_bbox = [x0, item_y0, x1, item_y1]
        result.append({
            "text": item,
            "bbox": item_bbox
        })

    return result





def create_boox(para_content, para_block, page_idx, page_size):
    page_width, page_height = page_size
    para_bbox = para_block.get('bbox')
    if para_bbox:
        x0, y0, x1, y1 = para_bbox
        para_content['bbox'] = [
            int(x0 * 1000 / page_width),
            int(y0 * 1000 / page_height),
            int(x1 * 1000 / page_width),
            int(y1 * 1000 / page_height),
        ]

    para_content['page_idx'] = page_idx
    return para_content


"""
    处理table_body的信息
"""
def make_table_body(block, para_block, img_buket_path, page_idx, page_size, output_content):
    table_body_para_content = {
        'type': BlockType.TABLE_BODY,
        'text':''
    }
    for line in block['lines']:
        for span in line['spans']:
            if span['type'] == ContentType.TABLE:
                if span.get('html', ''):
                    table_body_para_content['text'] = f"{span['html']}"
                if span.get('image_path', ''):
                    table_body_para_content['img_path'] = f"{img_buket_path}/{span['image_path']}"
    table_body_para_content['chunk_id'] = str(uuid.uuid4())
    block['chunk_id'] = str(uuid.uuid4())
    table_body_para_content = create_boox(table_body_para_content, para_block, page_idx, page_size)
    table_body_para_content['htmlbody'] = {
        'html': table_body_para_content['text'],
        'bodys':get_table_cells_from_html(table_body_para_content['bbox'],table_body_para_content['text'])
    }
    output_content.append(table_body_para_content)


"""
    处理table_caption的信息
"""


def make_table_caption(block, para_block, img_buket_path, page_idx, page_size, output_content):
    # table_caption
    table_caption_para_content = {
        'type': BlockType.TABLE_CAPTION,
        'text': merge_para_with_text(block),
    }
    table_caption_para_content['chunk_id'] = str(uuid.uuid4())
    block['chunk_id'] = str(uuid.uuid4())
    table_caption_para_content = create_boox(table_caption_para_content, block, page_idx, page_size)

    output_content.append(table_caption_para_content)


"""
    处理table_footnote的信息
"""


def make_table_footnote(block, para_block, img_buket_path, page_idx, page_size, output_content):
    # table_footnote
    table_footnote_para_content = {
        'type': BlockType.TABLE_FOOTNOTE,
        'text': merge_para_with_text(block),
    }
    table_footnote_para_content['chunk_id'] = str(uuid.uuid4())
    block['chunk_id'] = str(uuid.uuid4())
    table_footnote_para_content = create_boox(table_footnote_para_content, block, page_idx, page_size)

    output_content.append(table_footnote_para_content)


def parse_html_table(html_table_str):
    """
    解析 HTML 表格字符串，提取 行数、列数、单元格数据
    :param html_table_str: 包含 <table><tr><td> 的字符串
    :return: rows(行数), cols(列数), table_data(二维数据)
    """
    soup = BeautifulSoup(html_table_str, "html.parser")
    table = soup.find("table")
    rows_data = []

    # 遍历所有行 <tr>
    for tr in table.find_all("tr"):
        row = []
        # 遍历所有单元格 <td>
        for td in tr.find_all("td"):
            row.append(td.get_text(strip=True))  # 提取单元格文本
        if row:
            rows_data.append(row)

    if not rows_data:
        return 0, 0, []

    rows = len(rows_data)
    cols = max(len(row) for row in rows_data)  # 取最大列数
    return rows, cols, rows_data


def get_table_cells_from_html(table_bbox, html_table_str):

    if html_table_str is None or html_table_str == '':
        return []
    """
    从 HTML 表格字符串 + 表格整体bbox，计算所有单元格的 bbox 和数据
    """
    # 1. 解析 HTML 得到行数、列数、数据
    rows, cols, table_data = parse_html_table(html_table_str)

    # 2. 拆解表格 bbox
    x1_table, y1_table, x2_table, y2_table = table_bbox
    table_w = x2_table - x1_table
    table_h = y2_table - y1_table

    # 3. 单元格宽高
    cell_w = table_w / cols
    cell_h = table_h / rows

    cells = []

    # 4. 遍历每个单元格
    for row_idx in range(rows):
        for col_idx in range(cols):
            # 计算 bbox
            x1 = x1_table + col_idx * cell_w
            y1 = y1_table + row_idx * cell_h
            x2 = x1 + cell_w
            y2 = y1 + cell_h

            # 单元格数据
            try:
                data = table_data[row_idx][col_idx]
            except IndexError:
                data = ""

            cells.append({
                "row": row_idx + 1,
                "col": col_idx + 1,
                "bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                "data": data
            })

    return cells